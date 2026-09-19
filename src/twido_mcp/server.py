import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import serial.tools.list_ports
from pymodbus.client import ModbusSerialClient, ModbusTcpClient

import mcp.types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server


# ============================================================
# LOGGING
# ============================================================
# MCP stdio reserves stdout for JSON-RPC. Diagnostics MUST go
# to stderr.
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("twido-modbus-mcp")


# ============================================================
# CONSTANTS
# ============================================================

SERVER_NAME = "twido-modbus-mcp"
SERVER_VERSION = "2.0.0"

# These are the parameters that matched the working Twido port in
# the current commissioning setup. They can still be overridden per call.
DEFAULT_BAUDRATE = 19200
DEFAULT_PARITY = "N"
DEFAULT_STOPBITS = 1
DEFAULT_BYTESIZE = 8
DEFAULT_TCP_PORT = 502
DEFAULT_SLAVE_ID = 1

MAX_READ_BITS = 2000
MAX_READ_REGISTERS = 125
MAX_MAPPING_POINTS = 256

# Modbus maps used by Twido:
#   FC01 -> %M / output-coil area
#   FC03 -> %MW / holding-register area
# The physical %I/%Q I/O are deliberately NOT treated as directly
# writable physical outputs here. See the tool descriptions below.


# ============================================================
# IN-MEMORY SUPERVISED MAPPING STATE
# ============================================================

# Sessions deliberately live in the MCP process. A session is a
# sequential workflow: the server will not advance to the next point
# until the current point has been explicitly recorded by a human.
MAPPING_SESSIONS: dict[str, dict[str, Any]] = {}


# ============================================================
# GENERIC HELPERS
# ============================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def success_result(data: Any) -> types.CallToolResult:
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(data, indent=2, default=str),
            )
        ],
        is_error=False,
    )


def error_result(
    message: str,
    *,
    error: Exception | None = None,
    details: Any | None = None,
) -> types.CallToolResult:
    payload: dict[str, Any] = {
        "status": "error",
        "message": message,
    }
    if error is not None:
        payload["error"] = str(error)
        payload["error_type"] = type(error).__name__
    if details is not None:
        payload["details"] = details

    logger.error("%s | error=%s", message, str(error) if error else "")
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(payload, indent=2, default=str),
            )
        ],
        is_error=True,
    )


def get_arguments(params: types.CallToolRequestParams) -> dict[str, Any]:
    arguments = params.arguments
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise TypeError(
            f"MCP arguments must be an object/dictionary, got {type(arguments).__name__}"
        )
    return arguments


def require_connection_arguments(args: dict[str, Any]) -> tuple[str, str]:
    connection_type = str(args.get("connection_type", "")).strip().lower()
    endpoint = str(args.get("endpoint", "")).strip()
    if connection_type not in {"tcp", "serial"}:
        raise ValueError("connection_type must be either 'tcp' or 'serial'")
    if not endpoint:
        raise ValueError("endpoint must not be empty")
    return connection_type, endpoint


def get_serial_parameters(args: dict[str, Any]) -> dict[str, Any]:
    baudrate = int(args.get("baudrate", DEFAULT_BAUDRATE))
    parity = str(args.get("parity", DEFAULT_PARITY)).upper()
    stopbits = int(args.get("stopbits", DEFAULT_STOPBITS))
    bytesize = int(args.get("bytesize", DEFAULT_BYTESIZE))

    if baudrate <= 0:
        raise ValueError("baudrate must be greater than zero")
    if parity not in {"N", "E", "O"}:
        raise ValueError("parity must be one of: N, E, O")
    if stopbits not in {1, 2}:
        raise ValueError("stopbits must be either 1 or 2")
    if bytesize not in {7, 8}:
        raise ValueError("bytesize must be either 7 or 8")

    return {
        "baudrate": baudrate,
        "parity": parity,
        "stopbits": stopbits,
        "bytesize": bytesize,
    }


def get_slave_id(args: dict[str, Any]) -> int:
    slave_id = int(args.get("slave_id", DEFAULT_SLAVE_ID))
    if slave_id < 0 or slave_id > 247:
        raise ValueError("slave_id must be between 0 and 247")
    return slave_id


def get_modbus_client(
    connection_type: str,
    endpoint: str,
    baudrate: int = DEFAULT_BAUDRATE,
    parity: str = DEFAULT_PARITY,
    stopbits: int = DEFAULT_STOPBITS,
    bytesize: int = DEFAULT_BYTESIZE,
):
    logger.info(
        "Creating Modbus client: type=%s endpoint=%s baudrate=%s parity=%s stopbits=%s bytesize=%s",
        connection_type,
        endpoint,
        baudrate,
        parity,
        stopbits,
        bytesize,
    )

    if connection_type == "serial":
        return ModbusSerialClient(
            port=endpoint,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            bytesize=bytesize,
        )

    if connection_type == "tcp":
        return ModbusTcpClient(host=endpoint, port=DEFAULT_TCP_PORT)

    raise ValueError(f"Unsupported connection_type '{connection_type}'")


def connect_or_raise(client: Any, connection_type: str, endpoint: str) -> None:
    connected = client.connect()
    logger.info("Modbus connect returned: %s", connected)
    if not connected:
        raise ConnectionError(
            f"Failed to connect to PLC via {connection_type} endpoint {endpoint}"
        )


def check_modbus_result(result: Any, operation: str) -> None:
    if result is None:
        raise RuntimeError(f"{operation} returned no result")
    try:
        is_error = result.isError()
    except AttributeError as exc:
        raise RuntimeError(
            f"{operation} returned an unexpected object: {type(result).__name__}"
        ) from exc
    if is_error:
        raise RuntimeError(f"{operation} failed: {result}")


def close_client(client: Any | None) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception as exc:
        logger.warning("Error closing Modbus client: %s", exc)


def normalize_bool_list(values: list[Any]) -> list[bool]:
    return [bool(v) for v in values]


def write_json_file(filepath: str, payload: Any) -> Path:
    path = Path(filepath).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def build_connection_metadata(args: dict[str, Any]) -> dict[str, Any]:
    connection_type, endpoint = require_connection_arguments(args)
    serial_params = get_serial_parameters(args)
    return {
        "connection_type": connection_type,
        "endpoint": endpoint,
        "slave_id": get_slave_id(args),
        "serial": serial_params if connection_type == "serial" else None,
        "tcp_port": DEFAULT_TCP_PORT if connection_type == "tcp" else None,
    }


# ============================================================
# TOOL DEFINITIONS
# ============================================================

LIST_PORTS_TOOL = types.Tool(
    name="list_available_serial_ports",
    description=(
        "Lists serial/USB COM ports detected on the host. Use this first when "
        "the Twido is connected by USB/RS485."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)

INSPECT_PLC_TOOL = types.Tool(
    name="inspect_plc",
    description=(
        "Connects to the Twido and performs a non-destructive Modbus inspection. "
        "Reads the %M coil area and attempts to read the %MW holding-register area. "
        "A reset/empty PLC is a valid result: if %MW is not allocated, the tool "
        "reports that explicitly instead of treating it as a connection failure. "
        "This tool does NOT claim to retrieve the TwidoSuite program itself."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connection_type": {"type": "string", "enum": ["tcp", "serial"]},
            "endpoint": {"type": "string"},
            "slave_id": {"type": "integer", "minimum": 0, "maximum": 247, "default": 1},
            "m_bits": {"type": "integer", "minimum": 1, "maximum": MAX_READ_BITS, "default": 32},
            "mw_words": {"type": "integer", "minimum": 0, "maximum": MAX_READ_REGISTERS, "default": 32},
            "baudrate": {"type": "integer", "minimum": 1, "default": DEFAULT_BAUDRATE},
            "parity": {"type": "string", "enum": ["N", "E", "O"], "default": DEFAULT_PARITY},
            "stopbits": {"type": "integer", "enum": [1, 2], "default": DEFAULT_STOPBITS},
            "bytesize": {"type": "integer", "enum": [7, 8], "default": DEFAULT_BYTESIZE},
        },
        "required": ["connection_type", "endpoint"],
        "additionalProperties": False,
    },
)

READ_MEMORY_TOOL = types.Tool(
    name="read_plc_memory",
    description=(
        "Reads Modbus memory areas intentionally exposed by a Twido application. "
        "area='M' uses FC01 and corresponds to %M bits. area='MW' uses FC03 and "
        "corresponds to %MW words. This is runtime memory, not the TwidoSuite program."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connection_type": {"type": "string", "enum": ["tcp", "serial"]},
            "endpoint": {"type": "string"},
            "slave_id": {"type": "integer", "minimum": 0, "maximum": 247, "default": 1},
            "area": {"type": "string", "enum": ["M", "MW"]},
            "start_address": {"type": "integer", "minimum": 0, "default": 0},
            "count": {"type": "integer", "minimum": 1, "default": 10},
            "baudrate": {"type": "integer", "minimum": 1, "default": DEFAULT_BAUDRATE},
            "parity": {"type": "string", "enum": ["N", "E", "O"], "default": DEFAULT_PARITY},
            "stopbits": {"type": "integer", "enum": [1, 2], "default": DEFAULT_STOPBITS},
            "bytesize": {"type": "integer", "enum": [7, 8], "default": DEFAULT_BYTESIZE},
        },
        "required": ["connection_type", "endpoint", "area"],
        "additionalProperties": False,
    },
)

READ_PLC_STATE_COMPAT_TOOL = types.Tool(
    name="read_plc_state",
    description=(
        "Backward-compatible alias for reading Twido %MW holding registers. "
        "This is runtime memory only; an empty/reset PLC may legitimately reject "
        "the requested %MW range with Modbus exception 02."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connection_type": {"type": "string", "enum": ["tcp", "serial"]},
            "endpoint": {"type": "string"},
            "slave_id": {"type": "integer", "minimum": 0, "maximum": 247, "default": 1},
            "start_address": {"type": "integer", "minimum": 0, "default": 0},
            "count": {"type": "integer", "minimum": 1, "maximum": MAX_READ_REGISTERS, "default": 10},
            "baudrate": {"type": "integer", "minimum": 1, "default": DEFAULT_BAUDRATE},
            "parity": {"type": "string", "enum": ["N", "E", "O"], "default": DEFAULT_PARITY},
            "stopbits": {"type": "integer", "enum": [1, 2], "default": DEFAULT_STOPBITS},
            "bytesize": {"type": "integer", "enum": [7, 8], "default": DEFAULT_BYTESIZE},
        },
        "required": ["connection_type", "endpoint"],
        "additionalProperties": False,
    },
)


CREATE_BACKUP_TOOL = types.Tool(
    name="create_plc_backup",
    description=(
        "Creates a non-destructive runtime snapshot. It records connection settings, "
        "%M values, and %MW values when those areas are allocated. It also records the "
        "important limitation that the TwidoSuite application/program is not retrieved "
        "by Modbus. Use TwidoSuite itself to upload/save the project application."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connection_type": {"type": "string", "enum": ["tcp", "serial"]},
            "endpoint": {"type": "string"},
            "slave_id": {"type": "integer", "minimum": 0, "maximum": 247, "default": 1},
            "filepath": {"type": "string", "default": "twido_runtime_backup.json"},
            "m_bits": {"type": "integer", "minimum": 1, "maximum": MAX_READ_BITS, "default": 32},
            "mw_words": {"type": "integer", "minimum": 0, "maximum": MAX_READ_REGISTERS, "default": 32},
            "baudrate": {"type": "integer", "minimum": 1, "default": DEFAULT_BAUDRATE},
            "parity": {"type": "string", "enum": ["N", "E", "O"], "default": DEFAULT_PARITY},
            "stopbits": {"type": "integer", "enum": [1, 2], "default": DEFAULT_STOPBITS},
            "bytesize": {"type": "integer", "enum": [7, 8], "default": DEFAULT_BYTESIZE},
        },
        "required": ["connection_type", "endpoint"],
        "additionalProperties": False,
    },
)

START_MAPPING_TOOL = types.Tool(
    name="start_io_mapping",
    description=(
        "Starts a strictly sequential, human-supervised I/O mapping session. "
        "The server never tests multiple points in parallel and never advances "
        "automatically. Physical output actuation is deliberately NOT performed by "
        "Modbus because Twido Modbus coils map to %M, not physical %Q outputs. "
        "The returned session is a controlled checklist for human/TwidoSuite testing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "input_count": {"type": "integer", "minimum": 0, "maximum": MAX_MAPPING_POINTS, "default": 0},
            "output_count": {"type": "integer", "minimum": 0, "maximum": MAX_MAPPING_POINTS, "default": 0},
            "filepath": {"type": "string", "default": "twido_io_mapping.json"},
            "notes": {"type": "string", "default": ""},
        },
        "required": ["input_count", "output_count"],
        "additionalProperties": False,
    },
)

GET_MAPPING_TOOL = types.Tool(
    name="get_io_mapping_status",
    description=(
        "Returns the current state of a supervised I/O mapping session and the single "
        "next point to test. It never changes a PLC output."
    ),
    input_schema={
        "type": "object",
        "properties": {"session_id": {"type": "string"}},
        "required": ["session_id"],
        "additionalProperties": False,
    },
)

RECORD_MAPPING_TOOL = types.Tool(
    name="record_io_mapping_result",
    description=(
        "Records the human result for the current I/O mapping step. The server rejects "
        "a result for a different point, or an attempt to advance before the current "
        "step is recorded. This tool only records observations; it does not actuate hardware."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "point_type": {"type": "string", "enum": ["input", "output"]},
            "index": {"type": "integer", "minimum": 0},
            "observed": {"type": "boolean"},
            "human_confirmation": {"type": "boolean"},
            "label": {"type": "string", "default": ""},
            "notes": {"type": "string", "default": ""},
        },
        "required": ["session_id", "point_type", "index", "observed", "human_confirmation"],
        "additionalProperties": False,
    },
)

PREPARE_APP_TOOL = types.Tool(
    name="prepare_twidosuite_application_spec",
    description=(
        "Creates a human-reviewable JSON application specification for a TwidoSuite "
        "project from an explicit user description. It does NOT generate or modify a "
        "native .XPR/.XAR/.TWD project and does NOT download anything to the PLC. "
        "This boundary is intentional because no documented TwidoSuite automation API "
        "is assumed by this MCP."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filepath": {"type": "string", "default": "twido_application_spec.json"},
            "description": {"type": "string"},
            "inputs": {"type": "array", "items": {"type": "object"}, "default": []},
            "outputs": {"type": "array", "items": {"type": "object"}, "default": []},
            "logic": {"type": "array", "items": {"type": "object"}, "default": []},
            "human_approved": {"type": "boolean", "default": False},
        },
        "required": ["description"],
        "additionalProperties": False,
    },
)

OPEN_PROJECT_GUIDANCE_TOOL = types.Tool(
    name="twidosuite_project_guidance",
    description=(
        "Returns the safe manual steps needed to upload an existing Twido application "
        "from the controller into TwidoSuite, save it as a project, and use it for the "
        "program/diagram side of the workflow."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)


# ============================================================
# MCP TOOL LIST
# ============================================================

ALL_TOOLS = [
    LIST_PORTS_TOOL,
    READ_PLC_STATE_COMPAT_TOOL,
    INSPECT_PLC_TOOL,
    READ_MEMORY_TOOL,
    CREATE_BACKUP_TOOL,
    START_MAPPING_TOOL,
    GET_MAPPING_TOOL,
    RECORD_MAPPING_TOOL,
    PREPARE_APP_TOOL,
    OPEN_PROJECT_GUIDANCE_TOOL,
]


async def handle_list_tools(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    logger.info("MCP tools/list request received")
    return types.ListToolsResult(tools=ALL_TOOLS)


# ============================================================
# TOOL: LIST SERIAL PORTS
# ============================================================

async def tool_list_serial_ports(args: dict[str, Any]) -> types.CallToolResult:
    try:
        ports = serial.tools.list_ports.comports()
        result = []
        for port in ports:
            result.append(
                {
                    "port": port.device,
                    "description": port.description,
                    "manufacturer": port.manufacturer,
                    "product": port.product,
                    "serial_number": port.serial_number,
                    "vid": port.vid,
                    "pid": port.pid,
                    "interface": port.interface,
                    "hwid": port.hwid,
                }
            )
        return success_result({
            "status": "success",
            "port_count": len(result),
            "ports": result,
        })
    except Exception as exc:
        return error_result("Failed to enumerate serial ports.", error=exc)


# ============================================================
# TOOL: INSPECT PLC
# ============================================================

async def tool_read_plc_state_compat(args: dict[str, Any]) -> types.CallToolResult:
    translated = dict(args)
    translated["area"] = "MW"
    translated["start_address"] = int(args.get("start_address", 0))
    translated["count"] = int(args.get("count", 10))
    return await tool_read_plc_memory(translated)


async def tool_inspect_plc(args: dict[str, Any]) -> types.CallToolResult:
    client = None
    try:
        connection_type, endpoint = require_connection_arguments(args)
        serial_params = get_serial_parameters(args)
        slave_id = get_slave_id(args)
        m_bits = int(args.get("m_bits", 32))
        mw_words = int(args.get("mw_words", 32))
        if m_bits < 1 or m_bits > MAX_READ_BITS:
            raise ValueError(f"m_bits must be between 1 and {MAX_READ_BITS}")
        if mw_words < 0 or mw_words > MAX_READ_REGISTERS:
            raise ValueError(f"mw_words must be between 0 and {MAX_READ_REGISTERS}")

        client = get_modbus_client(connection_type, endpoint, **serial_params)
        connect_or_raise(client, connection_type, endpoint)

        report: dict[str, Any] = {
            "status": "success",
            "timestamp": utc_now(),
            "connection": build_connection_metadata(args),
            "runtime_memory": {},
            "limitations": [
                "This inspection is non-destructive.",
                "It does not retrieve the TwidoSuite ladder/program application.",
                "A reset/empty PLC can legitimately have zero allocated %MW objects.",
            ],
        }

        # %M / FC01. This is internal memory, not physical Q outputs.
        try:
            result = client.read_coils(
                address=0,
                count=m_bits,
                device_id=slave_id,
            )
            check_modbus_result(result, "read %M coils")
            values = normalize_bool_list(list(result.bits)[:m_bits])
            report["runtime_memory"]["M"] = {
                "start_address": 0,
                "count": len(values),
                "values": values,
                "meaning": "%M internal/output-coil Modbus area; not physical %Q I/O",
            }
        except Exception as exc:
            report["runtime_memory"]["M"] = {
                "status": "unavailable",
                "error": str(exc),
            }

        # %MW / FC03. A zero/empty area is a valid PLC condition.
        if mw_words > 0:
            try:
                result = client.read_holding_registers(
                    address=0,
                    count=mw_words,
                    device_id=slave_id,
                )
                check_modbus_result(result, "read %MW holding registers")
                values = list(result.registers)
                report["runtime_memory"]["MW"] = {
                    "start_address": 0,
                    "count": len(values),
                    "values": values,
                    "allocated": True,
                    "meaning": "%MW holding-register Modbus area",
                }
            except Exception as exc:
                report["runtime_memory"]["MW"] = {
                    "allocated": False,
                    "status": "not_available_or_not_allocated",
                    "error": str(exc),
                    "interpretation": (
                        "If the PLC returns Modbus exception 02 here, the requested %MW "
                        "address range is not allocated/valid. This does not mean that the "
                        "serial connection failed."
                    ),
                }
        else:
            report["runtime_memory"]["MW"] = {
                "allocated": "not_tested",
                "status": "skipped",
            }

        return success_result(report)

    except Exception as exc:
        return error_result("PLC inspection failed.", error=exc)
    finally:
        close_client(client)


# ============================================================
# TOOL: READ PLC MEMORY
# ============================================================

async def tool_read_plc_memory(args: dict[str, Any]) -> types.CallToolResult:
    client = None
    try:
        connection_type, endpoint = require_connection_arguments(args)
        serial_params = get_serial_parameters(args)
        slave_id = get_slave_id(args)
        area = str(args["area"]).upper()
        start_address = int(args.get("start_address", 0))
        count = int(args.get("count", 10))

        if start_address < 0:
            raise ValueError("start_address must be >= 0")
        if count < 1:
            raise ValueError("count must be >= 1")
        if area == "M" and count > MAX_READ_BITS:
            raise ValueError(f"count cannot exceed {MAX_READ_BITS} for area M")
        if area == "MW" and count > MAX_READ_REGISTERS:
            raise ValueError(f"count cannot exceed {MAX_READ_REGISTERS} for area MW")

        client = get_modbus_client(connection_type, endpoint, **serial_params)
        connect_or_raise(client, connection_type, endpoint)

        if area == "M":
            result = client.read_coils(
                address=start_address,
                count=count,
                device_id=slave_id,
            )
            check_modbus_result(result, "read %M coils")
            values = normalize_bool_list(list(result.bits)[:count])
        else:
            result = client.read_holding_registers(
                address=start_address,
                count=count,
                device_id=slave_id,
            )
            check_modbus_result(result, "read %MW holding registers")
            values = list(result.registers)

        return success_result({
            "status": "success",
            "area": area,
            "start_address": start_address,
            "count": len(values),
            "values": values,
            "slave_id": slave_id,
            "endpoint": endpoint,
        })

    except Exception as exc:
        return error_result("PLC memory read failed.", error=exc)
    finally:
        close_client(client)


# ============================================================
# TOOL: CREATE RUNTIME BACKUP
# ============================================================

async def tool_create_plc_backup(args: dict[str, Any]) -> types.CallToolResult:
    client = None
    try:
        connection_type, endpoint = require_connection_arguments(args)
        serial_params = get_serial_parameters(args)
        slave_id = get_slave_id(args)
        filepath = str(args.get("filepath", "twido_runtime_backup.json")).strip()
        if not filepath:
            raise ValueError("filepath must not be empty")

        m_bits = int(args.get("m_bits", 32))
        mw_words = int(args.get("mw_words", 32))
        if m_bits < 1 or m_bits > MAX_READ_BITS:
            raise ValueError(f"m_bits must be between 1 and {MAX_READ_BITS}")
        if mw_words < 0 or mw_words > MAX_READ_REGISTERS:
            raise ValueError(f"mw_words must be between 0 and {MAX_READ_REGISTERS}")

        client = get_modbus_client(connection_type, endpoint, **serial_params)
        connect_or_raise(client, connection_type, endpoint)

        backup: dict[str, Any] = {
            "backup_type": "runtime_modbus_snapshot",
            "timestamp": utc_now(),
            "server": SERVER_NAME,
            "server_version": SERVER_VERSION,
            "connection": build_connection_metadata(args),
            "program_backup": {
                "included": False,
                "reason": (
                    "The TwidoSuite application is not retrievable by the Modbus memory "
                    "operations implemented here. Upload/save the controller application "
                    "with TwidoSuite for a true program backup."
                ),
            },
            "memory": {},
        }

        # %M snapshot.
        try:
            result = client.read_coils(address=0, count=m_bits, device_id=slave_id)
            check_modbus_result(result, "backup %M read")
            backup["memory"]["M"] = {
                "start_address": 0,
                "count": m_bits,
                "values": normalize_bool_list(list(result.bits)[:m_bits]),
            }
        except Exception as exc:
            backup["memory"]["M"] = {
                "status": "unavailable",
                "error": str(exc),
            }

        # %MW snapshot. Failure here is deliberately recorded, not treated as
        # proof that the PLC is empty or disconnected.
        if mw_words > 0:
            try:
                result = client.read_holding_registers(
                    address=0,
                    count=mw_words,
                    device_id=slave_id,
                )
                check_modbus_result(result, "backup %MW read")
                backup["memory"]["MW"] = {
                    "start_address": 0,
                    "count": mw_words,
                    "values": list(result.registers),
                }
            except Exception as exc:
                backup["memory"]["MW"] = {
                    "status": "not_allocated_or_unavailable",
                    "error": str(exc),
                }

        path = write_json_file(filepath, backup)
        return success_result({
            "status": "success",
            "filepath": str(path),
            "backup": backup,
        })

    except Exception as exc:
        return error_result("PLC runtime backup failed.", error=exc)
    finally:
        close_client(client)


# ============================================================
# TOOL: START SEQUENTIAL I/O MAPPING
# ============================================================

async def tool_start_io_mapping(args: dict[str, Any]) -> types.CallToolResult:
    try:
        input_count = int(args.get("input_count", 0))
        output_count = int(args.get("output_count", 0))
        filepath = str(args.get("filepath", "twido_io_mapping.json")).strip()
        notes = str(args.get("notes", ""))

        if input_count < 0 or input_count > MAX_MAPPING_POINTS:
            raise ValueError(f"input_count must be between 0 and {MAX_MAPPING_POINTS}")
        if output_count < 0 or output_count > MAX_MAPPING_POINTS:
            raise ValueError(f"output_count must be between 0 and {MAX_MAPPING_POINTS}")
        if input_count + output_count == 0:
            raise ValueError("At least one input or output is required")

        points = []
        for index in range(input_count):
            points.append({
                "point_type": "input",
                "index": index,
                "status": "pending",
                "label": "",
                "observed": None,
                "notes": "",
            })
        for index in range(output_count):
            points.append({
                "point_type": "output",
                "index": index,
                "status": "pending",
                "label": "",
                "observed": None,
                "notes": "",
            })

        session_id = str(uuid.uuid4())
        session = {
            "session_id": session_id,
            "created_at": utc_now(),
            "status": "active",
            "current_position": 0,
            "notes": notes,
            "points": points,
            "filepath": filepath,
            "safety": {
                "sequential": True,
                "parallel_tests_allowed": False,
                "automatic_physical_actuation": False,
                "human_confirmation_required": True,
            },
        }
        MAPPING_SESSIONS[session_id] = session

        # Save immediately so a newly created plan exists independently of the MCP UI.
        path = write_json_file(filepath, session)
        session["filepath"] = str(path)
        write_json_file(str(path), session)

        return success_result({
            "status": "success",
            "session_id": session_id,
            "next": session["points"][0],
            "safety": session["safety"],
            "message": (
                "Mapping session created. Test only the single returned point. "
                "After the human has completed/observed that test, call "
                "record_io_mapping_result before requesting the next point."
            ),
        })

    except Exception as exc:
        return error_result("Could not start I/O mapping session.", error=exc)


# ============================================================
# TOOL: GET MAPPING STATUS
# ============================================================

async def tool_get_mapping_status(args: dict[str, Any]) -> types.CallToolResult:
    try:
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            raise ValueError("session_id is required")
        session = MAPPING_SESSIONS.get(session_id)
        if session is None:
            raise KeyError(f"Unknown mapping session: {session_id}")

        position = int(session["current_position"])
        points = session["points"]
        next_point = points[position] if position < len(points) else None
        return success_result({
            "status": "success",
            "session_id": session_id,
            "mapping_status": session["status"],
            "current_position": position,
            "total_points": len(points),
            "next": next_point,
            "safety": session["safety"],
            "message": (
                "No PLC output is changed by this tool. For physical outputs, the "
                "human should perform the controlled test in TwidoSuite/approved "
                "commissioning procedure, then record the observation here."
            ),
        })
    except Exception as exc:
        return error_result("Could not read mapping status.", error=exc)


# ============================================================
# TOOL: RECORD MAPPING RESULT
# ============================================================

async def tool_record_mapping_result(args: dict[str, Any]) -> types.CallToolResult:
    try:
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            raise ValueError("session_id is required")
        session = MAPPING_SESSIONS.get(session_id)
        if session is None:
            raise KeyError(f"Unknown mapping session: {session_id}")

        if session["status"] != "active":
            raise ValueError(f"Mapping session is not active: {session['status']}")

        point_type = str(args["point_type"]).lower()
        index = int(args["index"])
        observed = bool(args["observed"])
        human_confirmation = args.get("human_confirmation")
        label = str(args.get("label", ""))
        notes = str(args.get("notes", ""))

        if human_confirmation is not True:
            raise PermissionError(
                "human_confirmation must explicitly be true before a mapping step can be recorded"
            )

        position = int(session["current_position"])
        points = session["points"]
        if position >= len(points):
            raise ValueError("Mapping session is already complete")

        current = points[position]
        if current["point_type"] != point_type or int(current["index"]) != index:
            raise ValueError(
                "Sequential mapping violation: the requested point is not the current point. "
                f"Expected {current['point_type']} {current['index']}, received {point_type} {index}."
            )

        current["status"] = "confirmed" if observed else "not_confirmed"
        current["observed"] = observed
        current["label"] = label
        current["notes"] = notes
        current["completed_at"] = utc_now()
        session["current_position"] = position + 1

        if session["current_position"] >= len(points):
            session["status"] = "complete"
            next_point = None
        else:
            next_point = points[session["current_position"]]

        path = write_json_file(session["filepath"], session)

        return success_result({
            "status": "success",
            "session_id": session_id,
            "recorded": current,
            "mapping_status": session["status"],
            "next": next_point,
            "filepath": str(path),
        })

    except Exception as exc:
        return error_result("Could not record mapping result.", error=exc)


# ============================================================
# TOOL: PREPARE TWIDOSUITE APPLICATION SPECIFICATION
# ============================================================

async def tool_prepare_application_spec(args: dict[str, Any]) -> types.CallToolResult:
    try:
        description = str(args.get("description", "")).strip()
        if not description:
            raise ValueError("description is required")

        filepath = str(args.get("filepath", "twido_application_spec.json")).strip()
        if not filepath:
            raise ValueError("filepath must not be empty")

        human_approved = bool(args.get("human_approved", False))
        inputs = args.get("inputs", [])
        outputs = args.get("outputs", [])
        logic = args.get("logic", [])

        if not isinstance(inputs, list) or not isinstance(outputs, list) or not isinstance(logic, list):
            raise TypeError("inputs, outputs, and logic must be arrays")

        spec = {
            "specification_type": "twido_application_review_spec",
            "created_at": utc_now(),
            "server": SERVER_NAME,
            "server_version": SERVER_VERSION,
            "human_approved": human_approved,
            "description": description,
            "inputs": inputs,
            "outputs": outputs,
            "logic": logic,
            "constraints": {
                "requires_human_review_before_download": True,
                "download_performed_by_this_tool": False,
                "native_twidosuite_project_generated": False,
            },
            "twidosuite_note": (
                "Open/recreate this reviewed specification in TwidoSuite. The MCP does "
                "not fabricate a native XPR/XAR/TWD file because that file format and "
                "the TwidoSuite GUI/programming interface are not assumed to be a stable "
                "public automation API."
            ),
        }
        path = write_json_file(filepath, spec)

        return success_result({
            "status": "success",
            "filepath": str(path),
            "specification": spec,
        })

    except Exception as exc:
        return error_result("Could not create TwidoSuite application specification.", error=exc)


# ============================================================
# TOOL: TWIDOSUITE GUIDANCE
# ============================================================

async def tool_twidosuite_guidance(args: dict[str, Any]) -> types.CallToolResult:
    return success_result({
        "status": "success",
        "workflow": [
            "Connect the PC to the Twido with the appropriate Schneider programming cable.",
            "Open TwidoSuite in Programming mode.",
            "Use Project -> Open an existing project -> From Controller -> Load to upload an existing application.",
            "Disconnect from the controller before saving the uploaded project, then use Save current project.",
            "Review hardware, I/O, memory objects, symbols, and ladder/list program before any modification.",
            "For a new/reset PLC, create the application in TwidoSuite and review it before downloading.",
            "Keep the physical I/O mapping workflow sequential and require a human observation after each test.",
        ],
        "important_boundary": (
            "This MCP does not pretend that a Modbus FC01 write to a coil is a physical %Q output test. "
            "On Twido, Modbus coil access corresponds to %M. Physical I/O/program transfer belongs to "
            "the TwidoSuite/programming side of the workflow."
        ),
    })


# ============================================================
# MCP CALL DISPATCHER
# ============================================================

async def handle_call_tool(
    ctx: ServerRequestContext,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    try:
        if params is None:
            return error_result("MCP call_tool request parameters are missing.")

        name = params.name
        logger.info("MCP tools/call received: %s", name)
        try:
            args = get_arguments(params)
        except Exception as exc:
            return error_result("Invalid MCP tool arguments.", error=exc)

        if name == "list_available_serial_ports":
            return await tool_list_serial_ports(args)
        if name == "read_plc_state":
            return await tool_read_plc_state_compat(args)
        if name == "inspect_plc":
            return await tool_inspect_plc(args)
        if name == "read_plc_memory":
            return await tool_read_plc_memory(args)
        if name == "create_plc_backup":
            return await tool_create_plc_backup(args)
        if name == "start_io_mapping":
            return await tool_start_io_mapping(args)
        if name == "get_io_mapping_status":
            return await tool_get_mapping_status(args)
        if name == "record_io_mapping_result":
            return await tool_record_mapping_result(args)
        if name == "prepare_twidosuite_application_spec":
            return await tool_prepare_application_spec(args)
        if name == "twidosuite_project_guidance":
            return await tool_twidosuite_guidance(args)

        return error_result(
            f"Unknown MCP tool: {name}",
            details={"available_tools": [tool.name for tool in ALL_TOOLS]},
        )

    except Exception as exc:
        logger.exception("Unhandled exception in MCP call handler")
        return error_result("Unhandled exception in MCP tool handler.", error=exc)


# ============================================================
# SERVER
# ============================================================

app = Server(
    SERVER_NAME,
    version=SERVER_VERSION,
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


async def run_server() -> None:
    logger.info("Starting %s version %s", SERVER_NAME, SERVER_VERSION)
    async with stdio_server() as (read_stream, write_stream):
        logger.info("MCP stdio transport started")
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )
    logger.info("MCP stdio transport stopped")


def main() -> None:
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception:
        logger.exception("Fatal MCP server error")
        raise


if __name__ == "__main__":
    main()
