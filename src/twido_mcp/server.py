import asyncio
import json
import logging
import sys
import time
from typing import Any

import serial.tools.list_ports
from pymodbus.client import ModbusTcpClient, ModbusSerialClient

import mcp.types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server


# ============================================================
# LOGGING
# ============================================================
#
# IMPORTANT:
# MCP stdio uses stdout for the JSON-RPC protocol.
# Therefore NEVER use print() for diagnostics.
# Everything diagnostic goes to stderr.
#

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
SERVER_VERSION = "1.0.0"

DEFAULT_BAUDRATE = 19200
DEFAULT_PARITY = "N"
DEFAULT_STOPBITS = 1
DEFAULT_BYTESIZE = 8

DEFAULT_TCP_PORT = 502

BACKUP_START_ADDRESS = 0
BACKUP_REGISTER_COUNT = 101

MAX_READ_REGISTERS = 125
MAX_OUTPUT_INDEX = 15


# ============================================================
# GENERIC MCP RESULT HELPERS
# ============================================================

def success_result(data: Any) -> types.CallToolResult:
    """
    Create a successful MCP tool result containing JSON text.
    """

    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(
                    data,
                    indent=2,
                    default=str,
                ),
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
    """
    Create a consistent MCP error result.

    The exception is converted to text rather than being allowed
    to escape through the MCP handler.
    """

    payload: dict[str, Any] = {
        "status": "error",
        "message": message,
    }

    if error is not None:
        payload["error"] = str(error)
        payload["error_type"] = type(error).__name__

    if details is not None:
        payload["details"] = details

    logger.error(
        "%s | error=%s",
        message,
        str(error) if error else "",
    )

    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(
                    payload,
                    indent=2,
                    default=str,
                ),
            )
        ],
        is_error=True,
    )


# ============================================================
# ARGUMENT VALIDATION
# ============================================================

def get_arguments(
    params: types.CallToolRequestParams,
) -> dict[str, Any]:
    """
    Safely extract MCP tool arguments.

    The MCP SDK normally supplies a dictionary, but this function
    deliberately validates the value before using it.
    """

    arguments = params.arguments

    if arguments is None:
        return {}

    if not isinstance(arguments, dict):
        raise TypeError(
            f"MCP arguments must be an object/dictionary, "
            f"got {type(arguments).__name__}"
        )

    return arguments


def require_connection_arguments(
    args: dict[str, Any],
) -> tuple[str, str]:
    """
    Validate connection_type and endpoint.
    """

    connection_type = args.get("connection_type")
    endpoint = args.get("endpoint")

    if connection_type is None:
        raise ValueError(
            "Missing required parameter: connection_type"
        )

    if endpoint is None:
        raise ValueError(
            "Missing required parameter: endpoint"
        )

    connection_type = str(connection_type).strip().lower()
    endpoint = str(endpoint).strip()

    if connection_type not in {"tcp", "serial"}:
        raise ValueError(
            "connection_type must be either 'tcp' or 'serial'"
        )

    if not endpoint:
        raise ValueError(
            "endpoint must not be empty"
        )

    return connection_type, endpoint


def get_serial_parameters(
    args: dict[str, Any],
) -> dict[str, Any]:
    """
    Extract and validate serial parameters.
    """

    baudrate = int(
        args.get(
            "baudrate",
            DEFAULT_BAUDRATE,
        )
    )

    parity = str(
        args.get(
            "parity",
            DEFAULT_PARITY,
        )
    ).upper()

    stopbits = int(
        args.get(
            "stopbits",
            DEFAULT_STOPBITS,
        )
    )

    bytesize = int(
        args.get(
            "bytesize",
            DEFAULT_BYTESIZE,
        )
    )

    if baudrate <= 0:
        raise ValueError("baudrate must be greater than zero")

    if parity not in {"N", "E", "O"}:
        raise ValueError(
            "parity must be one of: N, E, O"
        )

    if stopbits not in {1, 2}:
        raise ValueError(
            "stopbits must be either 1 or 2"
        )

    if bytesize not in {7, 8}:
        raise ValueError(
            "bytesize must be either 7 or 8"
        )

    return {
        "baudrate": baudrate,
        "parity": parity,
        "stopbits": stopbits,
        "bytesize": bytesize,
    }


# ============================================================
# MODBUS CONNECTION HELPER
# ============================================================

def get_modbus_client(
    connection_type: str,
    endpoint: str,
    baudrate: int = DEFAULT_BAUDRATE,
    parity: str = DEFAULT_PARITY,
    stopbits: int = DEFAULT_STOPBITS,
    bytesize: int = DEFAULT_BYTESIZE,
):
    """
    Create a Modbus TCP or Modbus RTU client.

    connection_type:
        tcp
            endpoint = IP address

        serial
            endpoint = COM port such as COM5
    """

    connection_type = str(
        connection_type
    ).strip().lower()

    endpoint = str(endpoint).strip()

    logger.info(
        "Creating Modbus client: type=%s endpoint=%s",
        connection_type,
        endpoint,
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

        return ModbusTcpClient(
            host=endpoint,
            port=DEFAULT_TCP_PORT,
        )

    raise ValueError(
        f"Unsupported connection_type '{connection_type}'. "
        "Use 'serial' or 'tcp'."
    )


# ============================================================
# MODBUS RESULT CHECKING
# ============================================================

def check_modbus_result(
    result: Any,
    operation: str,
) -> None:
    """
    Raise a useful exception if a Modbus operation failed.
    """

    if result is None:
        raise RuntimeError(
            f"{operation} returned no result"
        )

    try:
        is_error = result.isError()
    except AttributeError:
        raise RuntimeError(
            f"{operation} returned an unexpected object: "
            f"{type(result).__name__}"
        )

    if is_error:
        raise RuntimeError(
            f"{operation} failed: {result}"
        )


# ============================================================
# MCP TOOL DEFINITIONS
# ============================================================

LIST_PORTS_TOOL = types.Tool(
    name="list_available_serial_ports",
    description=(
        "Lists all serial/USB COM ports detected on the host. "
        "Use this to identify the serial port connected to the "
        "Twido PLC communication adapter."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)


READ_PLC_STATE_TOOL = types.Tool(
    name="read_plc_state",
    description=(
        "Reads Modbus holding registers from the Twido PLC. "
        "For serial communication use an endpoint such as COM5. "
        "For TCP communication use the PLC IP address."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connection_type": {
                "type": "string",
                "enum": ["tcp", "serial"],
                "description": (
                    "PLC communication type."
                ),
            },
            "endpoint": {
                "type": "string",
                "description": (
                    "TCP IP address or serial COM port, "
                    "for example 192.168.1.10 or COM5."
                ),
            },
            "slave_id": {
                "type": "integer",
                "minimum": 0,
                "maximum": 247,
                "default": 1,
                "description": (
                    "Modbus RTU slave/unit ID of the Twido PLC."
                ),
            },
            "start_address": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": (
                    "First Modbus holding-register address."
                ),
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_READ_REGISTERS,
                "default": 10,
                "description": (
                    "Number of holding registers to read."
                ),
            },
            "baudrate": {
                "type": "integer",
                "minimum": 1,
                "default": DEFAULT_BAUDRATE,
            },
            "parity": {
                "type": "string",
                "enum": ["N", "E", "O"],
                "default": DEFAULT_PARITY,
            },
            "stopbits": {
                "type": "integer",
                "enum": [1, 2],
                "default": DEFAULT_STOPBITS,
            },
            "bytesize": {
                "type": "integer",
                "enum": [7, 8],
                "default": DEFAULT_BYTESIZE,
            },
        },
        "required": [
            "connection_type",
            "endpoint",
        ],
        "additionalProperties": False,
    },
)


CREATE_BACKUP_TOOL = types.Tool(
    name="create_plc_backup",
    description=(
        "Reads holding registers %MW0 through %MW100 "
        "and writes a timestamped JSON backup."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connection_type": {
                "type": "string",
                "enum": ["tcp", "serial"],
            },
            "endpoint": {
                "type": "string",
                "description": (
                    "PLC IP address or serial COM port."
                ),
            },
            "filepath": {
                "type": "string",
                "default": "twido_backup.json",
                "description": (
                    "Output JSON file path."
                ),
            },
            "baudrate": {
                "type": "integer",
                "minimum": 1,
                "default": DEFAULT_BAUDRATE,
            },
            "parity": {
                "type": "string",
                "enum": ["N", "E", "O"],
                "default": DEFAULT_PARITY,
            },
            "stopbits": {
                "type": "integer",
                "enum": [1, 2],
                "default": DEFAULT_STOPBITS,
            },
            "bytesize": {
                "type": "integer",
                "enum": [7, 8],
                "default": DEFAULT_BYTESIZE,
            },
        },
        "required": [
            "connection_type",
            "endpoint",
        ],
        "additionalProperties": False,
    },
)


TEST_OUTPUT_TOOL = types.Tool(
    name="test_single_output_series",
    description=(
        "Pulses one PLC output for 1.5 seconds and then "
        "turns it off. This changes physical PLC hardware and "
        "requires explicit human confirmation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connection_type": {
                "type": "string",
                "enum": ["tcp", "serial"],
            },
            "endpoint": {
                "type": "string",
            },
            "output_index": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_OUTPUT_INDEX,
                "description": (
                    "Output index. "
                    "0 corresponds to Modbus coil 0."
                ),
            },
            "human_confirmed": {
                "type": "boolean",
                "description": (
                    "Must explicitly be true before "
                    "physical PLC outputs are changed."
                ),
            },
            "baudrate": {
                "type": "integer",
                "minimum": 1,
                "default": DEFAULT_BAUDRATE,
            },
            "parity": {
                "type": "string",
                "enum": ["N", "E", "O"],
                "default": DEFAULT_PARITY,
            },
            "stopbits": {
                "type": "integer",
                "enum": [1, 2],
                "default": DEFAULT_STOPBITS,
            },
            "bytesize": {
                "type": "integer",
                "enum": [7, 8],
                "default": DEFAULT_BYTESIZE,
            },
        },
        "required": [
            "connection_type",
            "endpoint",
            "output_index",
            "human_confirmed",
        ],
        "additionalProperties": False,
    },
)


# ============================================================
# MCP LIST TOOLS HANDLER
# ============================================================

async def handle_list_tools(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    """
    Return the complete list of MCP tools.

    This is the low-level Server API:
        async (ctx, params) -> ListToolsResult
    """

    logger.info(
        "MCP tools/list request received"
    )

    return types.ListToolsResult(
        tools=[
            LIST_PORTS_TOOL,
            READ_PLC_STATE_TOOL,
            CREATE_BACKUP_TOOL,
            TEST_OUTPUT_TOOL,
        ]
    )


# ============================================================
# TOOL 1: LIST SERIAL PORTS
# ============================================================

async def tool_list_serial_ports(
    args: dict[str, Any],
) -> types.CallToolResult:

    try:

        logger.info(
            "Enumerating serial ports"
        )

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

        logger.info(
            "Found %d serial port(s)",
            len(result),
        )

        return success_result(
            {
                "status": "success",
                "port_count": len(result),
                "ports": result,
            }
        )

    except Exception as exc:

        return error_result(
            "Failed to enumerate serial ports.",
            error=exc,
        )


# ============================================================
# TOOL 2: READ PLC STATE
# ============================================================

async def tool_read_plc_state(
    args: dict[str, Any],
) -> types.CallToolResult:

    client = None

    try:

        connection_type, endpoint = (
            require_connection_arguments(args)
        )

        serial_params = get_serial_parameters(args)

        start_address = int(
            args.get(
                "start_address",
                0,
            )
        )

        count = int(
            args.get(
                "count",
                10,
            )
        )

        slave_id = int(
            args.get(
                "slave_id",
                1,
            )
        )

        if start_address < 0:
            raise ValueError(
                "start_address must be >= 0"
            )

        if count <= 0:
            raise ValueError(
                "count must be greater than zero"
            )

        if count > MAX_READ_REGISTERS:
            raise ValueError(
                f"count cannot exceed {MAX_READ_REGISTERS}"
            )

        logger.info(
            "PLC read requested: type=%s endpoint=%s "
            "address=%d count=%d",
            connection_type,
            endpoint,
            start_address,
            count,
        )

        client = get_modbus_client(
            connection_type=connection_type,
            endpoint=endpoint,
            **serial_params,
        )

        connected = client.connect()

        logger.info(
            "Modbus connect returned: %s",
            connected,
        )

        if not connected:

            return error_result(
                "Failed to connect to PLC.",
                details={
                    "connection_type": connection_type,
                    "endpoint": endpoint,
                    "hint": (
                        "Verify the COM port, serial parameters, "
                        "PLC address/unit configuration, and cable."
                    ),
                },
            )

        result = client.read_holding_registers(
            address=start_address,
            count=count,
            device_id=slave_id,
        )

        check_modbus_result(
            result,
            "read_holding_registers",
        )

        registers = list(
            result.registers
        )

        logger.info(
            "PLC read successful: %d registers",
            len(registers),
        )

        return success_result(
            {
                "status": "success",
                "connection_type": connection_type,
                "endpoint": endpoint,
                "start_address": start_address,
                "count": len(registers),
                "values": registers,
            }
        )

    except Exception as exc:

        return error_result(
            "Exception during Modbus read.",
            error=exc,
        )

    finally:

        if client is not None:

            try:
                client.close()

            except Exception as exc:

                logger.warning(
                    "Error closing Modbus client: %s",
                    exc,
                )


# ============================================================
# TOOL 3: CREATE PLC BACKUP
# ============================================================

async def tool_create_plc_backup(
    args: dict[str, Any],
) -> types.CallToolResult:

    client = None

    try:

        connection_type, endpoint = (
            require_connection_arguments(args)
        )

        serial_params = get_serial_parameters(args)

        filepath = str(
            args.get(
                "filepath",
                "twido_backup.json",
            )
        ).strip()

        if not filepath:
            raise ValueError(
                "filepath must not be empty"
            )

        logger.info(
            "PLC backup requested: type=%s endpoint=%s "
            "file=%s",
            connection_type,
            endpoint,
            filepath,
        )

        client = get_modbus_client(
            connection_type=connection_type,
            endpoint=endpoint,
            **serial_params,
        )

        connected = client.connect()

        logger.info(
            "Modbus connect returned: %s",
            connected,
        )

        if not connected:

            return error_result(
                "Failed to connect to PLC.",
                details={
                    "connection_type": connection_type,
                    "endpoint": endpoint,
                },
            )

        result = client.read_holding_registers(
            address=BACKUP_START_ADDRESS,
            count=BACKUP_REGISTER_COUNT,
        )

        check_modbus_result(
            result,
            "backup holding-register read",
        )

        registers = list(
            result.registers
        )

        backup_data = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            ),
            "server": SERVER_NAME,
            "server_version": SERVER_VERSION,
            "connection_type": connection_type,
            "endpoint": endpoint,
            "start_address": BACKUP_START_ADDRESS,
            "register_count": len(registers),
            "holding_registers": registers,
        }

        with open(
            filepath,
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                backup_data,
                file,
                indent=2,
            )

        logger.info(
            "PLC backup successfully written: %s",
            filepath,
        )

        return success_result(
            {
                "status": "success",
                "message": (
                    f"Backup successfully written "
                    f"to {filepath}"
                ),
                "filepath": filepath,
                "register_count": len(registers),
            }
        )

    except Exception as exc:

        return error_result(
            "PLC backup failed.",
            error=exc,
        )

    finally:

        if client is not None:

            try:
                client.close()

            except Exception as exc:

                logger.warning(
                    "Error closing Modbus client: %s",
                    exc,
                )


# ============================================================
# TOOL 4: TEST SINGLE PLC OUTPUT
# ============================================================

async def tool_test_single_output(
    args: dict[str, Any],
) -> types.CallToolResult:

    client = None

    output_index = None

    try:

        connection_type, endpoint = (
            require_connection_arguments(args)
        )

        serial_params = get_serial_parameters(args)

        if "output_index" not in args:
            raise ValueError(
                "output_index is required"
            )

        output_index = int(
            args["output_index"]
        )

        if (
            output_index < 0
            or output_index > MAX_OUTPUT_INDEX
        ):
            raise ValueError(
                "output_index must be between "
                f"0 and {MAX_OUTPUT_INDEX}"
            )

        human_confirmed = args.get(
            "human_confirmed",
            False,
        )

        if human_confirmed is not True:

            return error_result(
                "Physical PLC output test refused.",
                details=(
                    "human_confirmed must explicitly "
                    "be true."
                ),
            )

        logger.warning(
            "PHYSICAL PLC OUTPUT TEST requested: "
            "output=%d endpoint=%s",
            output_index,
            endpoint,
        )

        client = get_modbus_client(
            connection_type=connection_type,
            endpoint=endpoint,
            **serial_params,
        )

        connected = client.connect()

        if not connected:

            return error_result(
                "Connection failed before output test.",
                details={
                    "connection_type": connection_type,
                    "endpoint": endpoint,
                },
            )

        # ----------------------------------------------------
        # Reset first 16 coils.
        # ----------------------------------------------------

        for i in range(16):

            result = client.write_coil(
                address=i,
                value=False,
                device_id=slave_id,
            )


            check_modbus_result(
                result,
                f"reset coil {i}",
            )

        # ----------------------------------------------------
        # Turn selected output ON.
        # ----------------------------------------------------

        result = client.write_coil(
            address=output_index,
            value=True,
            device_id=slave_id,
        )


        check_modbus_result(
            result,
            f"turn output {output_index} ON",
        )

        logger.warning(
            "PLC output %d is now ON",
            output_index,
        )

        # ----------------------------------------------------
        # Keep output ON for 1.5 seconds.
        # ----------------------------------------------------

        await asyncio.sleep(1.5)

        # ----------------------------------------------------
        # Turn selected output OFF.
        # ----------------------------------------------------

        result = client.write_coil(
            address=output_index,
            value=True,
            device_id=slave_id,
        )

        check_modbus_result(
            result,
            f"turn output {output_index} OFF",
        )

        logger.warning(
            "PLC output %d has been turned OFF",
            output_index,
        )

        return success_result(
            {
                "status": "success",
                "message": (
                    f"PLC output {output_index} "
                    "was pulsed for 1.5 seconds "
                    "and then turned OFF."
                ),
                "output_index": output_index,
                "duration_seconds": 1.5,
            }
        )

    except Exception as exc:

        # ----------------------------------------------------
        # Safety attempt:
        # If something fails after the output was selected,
        # attempt to turn that output OFF.
        # ----------------------------------------------------

        if (
            client is not None
            and output_index is not None
        ):

            try:

                client.write_coil(
                    address=output_index,
                    value=False,
                )

                logger.warning(
                    "Safety cleanup: output %d "
                    "forced OFF after error.",
                    output_index,
                )

            except Exception as cleanup_exc:

                logger.error(
                    "Safety cleanup failed for output %d: %s",
                    output_index,
                    cleanup_exc,
                )

        return error_result(
            "PLC output test failed.",
            error=exc,
        )

    finally:

        if client is not None:

            try:
                client.close()

            except Exception as exc:

                logger.warning(
                    "Error closing Modbus client: %s",
                    exc,
                )


# ============================================================
# MCP TOOL CALL HANDLER
# ============================================================

async def handle_call_tool(
    ctx: ServerRequestContext,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:

    """
    Main MCP tool dispatcher.

    IMPORTANT:
    The low-level MCP Server API calls this as:

        handle_call_tool(ctx, params)

    The tool name is:

        params.name

    Arguments are:

        params.arguments
    """

    try:

        # ----------------------------------------------------
        # Validate the MCP request itself.
        # ----------------------------------------------------

        if params is None:
            return error_result(
                "MCP call_tool request parameters are missing."
            )

        name = params.name

        logger.info(
            "MCP tools/call received: %s",
            name,
        )

        try:

            args = get_arguments(params)

        except Exception as exc:

            return error_result(
                "Invalid MCP tool arguments.",
                error=exc,
            )

        logger.info(
            "Tool '%s' arguments: %s",
            name,
            args,
        )

        # ----------------------------------------------------
        # Dispatch tool.
        # ----------------------------------------------------

        if name == "list_available_serial_ports":

            return await tool_list_serial_ports(args)

        if name == "read_plc_state":

            return await tool_read_plc_state(args)

        if name == "create_plc_backup":

            return await tool_create_plc_backup(args)

        if name == "test_single_output_series":

            return await tool_test_single_output(args)

        return error_result(
            f"Unknown MCP tool: {name}",
            details={
                "available_tools": [
                    "list_available_serial_ports",
                    "read_plc_state",
                    "create_plc_backup",
                    "test_single_output_series",
                ]
            },
        )

    except Exception as exc:

        # ----------------------------------------------------
        # Last-resort protection.
        #
        # Nothing from this handler should be allowed to
        # escape as an unhandled Python exception.
        # ----------------------------------------------------

        logger.exception(
            "Unhandled exception in MCP call handler"
        )

        return error_result(
            "Unhandled exception in MCP tool handler.",
            error=exc,
        )


# ============================================================
# CREATE MCP SERVER
# ============================================================

app = Server(
    SERVER_NAME,
    version=SERVER_VERSION,
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


# ============================================================
# ASYNC SERVER RUNNER
# ============================================================

async def run_server() -> None:
    """
    Run MCP over stdio.

    stdout is reserved for MCP protocol traffic.
    Diagnostics go to stderr via logging.
    """

    logger.info(
        "Starting %s version %s",
        SERVER_NAME,
        SERVER_VERSION,
    )

    async with stdio_server() as (
        read_stream,
        write_stream,
    ):

        logger.info(
            "MCP stdio transport started"
        )

        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )

    logger.info(
        "MCP stdio transport stopped"
    )


# ============================================================
# ENTRY POINT
# ============================================================

def main() -> None:

    try:

        asyncio.run(
            run_server()
        )

    except KeyboardInterrupt:

        logger.info(
            "Server stopped by user"
        )

    except Exception:

        logger.exception(
            "Fatal MCP server error"
        )

        raise


if __name__ == "__main__":
    main()
