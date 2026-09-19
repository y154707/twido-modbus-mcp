import asyncio
import json
import time

import serial.tools.list_ports
from pymodbus.client import ModbusTcpClient, ModbusSerialClient

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
import mcp.types as types


# ============================================================
# Modbus connection helper
# ============================================================

def get_modbus_client(
    connection_type: str,
    endpoint: str,
    baudrate: int = 19200,
    parity: str = "N",
    stopbits: int = 1,
    bytesize: int = 8,
):
    """
    Create a Modbus TCP or RTU client.

    connection_type:
        "tcp"    -> endpoint is an IP address
        "serial" -> endpoint is a COM/serial port such as COM3
    """

    connection_type = connection_type.lower().strip()

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
            port=502,
        )

    raise ValueError(
        f"Unsupported connection_type '{connection_type}'. "
        "Use 'serial' or 'tcp'."
    )


# ============================================================
# MCP TOOL DEFINITIONS
# ============================================================

async def handle_list_tools(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    """
    Tell the MCP client which tools are available.
    """

    return types.ListToolsResult(
        tools=[
            # ------------------------------------------------
            # Tool 1: Scan serial/USB ports
            # ------------------------------------------------
            types.Tool(
                name="list_available_serial_ports",
                description=(
                    "Lists all serial/USB COM ports detected on the host. "
                    "Use this to identify the serial port connected to the "
                    "Twido PLC programming/communication adapter."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),

            # ------------------------------------------------
            # Tool 2: Read PLC state
            # ------------------------------------------------
            types.Tool(
                name="read_plc_state",
                description=(
                    "Reads Modbus holding registers from the Twido PLC. "
                    "For serial communication, endpoint should be a COM port "
                    "such as COM3. For TCP, endpoint should be the PLC IP address."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "connection_type": {
                            "type": "string",
                            "enum": ["tcp", "serial"],
                            "description": (
                                "PLC communication type: 'tcp' or 'serial'."
                            ),
                        },
                        "endpoint": {
                            "type": "string",
                            "description": (
                                "TCP IP address or serial COM port. "
                                "Examples: '192.168.1.10' or 'COM3'."
                            ),
                        },
                        "start_address": {
                            "type": "integer",
                            "default": 0,
                            "description": "First Modbus holding-register address.",
                        },
                        "count": {
                            "type": "integer",
                            "default": 10,
                            "description": "Number of holding registers to read.",
                        },
                        "baudrate": {
                            "type": "integer",
                            "default": 19200,
                            "description": "Serial baud rate when using serial communication.",
                        },
                        "parity": {
                            "type": "string",
                            "enum": ["N", "E", "O"],
                            "default": "N",
                            "description": "Serial parity.",
                        },
                        "stopbits": {
                            "type": "integer",
                            "enum": [1, 2],
                            "default": 1,
                            "description": "Serial stop bits.",
                        },
                        "bytesize": {
                            "type": "integer",
                            "enum": [7, 8],
                            "default": 8,
                            "description": "Serial data bits.",
                        },
                    },
                    "required": [
                        "connection_type",
                        "endpoint",
                    ],
                    "additionalProperties": False,
                },
            ),

            # ------------------------------------------------
            # Tool 3: PLC backup
            # ------------------------------------------------
            types.Tool(
                name="create_plc_backup",
                description=(
                    "Reads PLC holding registers %MW0-%MW100 and saves "
                    "a timestamped JSON backup."
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
                            "description": "Output JSON file path.",
                        },
                        "baudrate": {
                            "type": "integer",
                            "default": 19200,
                        },
                        "parity": {
                            "type": "string",
                            "enum": ["N", "E", "O"],
                            "default": "N",
                        },
                        "stopbits": {
                            "type": "integer",
                            "enum": [1, 2],
                            "default": 1,
                        },
                        "bytesize": {
                            "type": "integer",
                            "enum": [7, 8],
                            "default": 8,
                        },
                    },
                    "required": [
                        "connection_type",
                        "endpoint",
                    ],
                    "additionalProperties": False,
                },
            ),

            # ------------------------------------------------
            # Tool 4: Output test
            # ------------------------------------------------
            types.Tool(
                name="test_single_output_series",
                description=(
                    "Pulses one PLC output for 1.5 seconds and then "
                    "resets it. Requires explicit human confirmation."
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
                            "maximum": 15,
                            "description": (
                                "PLC output index. For example, 0 represents %Q0.0."
                            ),
                        },
                        "human_confirmed": {
                            "type": "boolean",
                            "description": (
                                "Must be true to allow physical PLC output changes."
                            ),
                        },
                        "baudrate": {
                            "type": "integer",
                            "default": 19200,
                        },
                        "parity": {
                            "type": "string",
                            "enum": ["N", "E", "O"],
                            "default": "N",
                        },
                        "stopbits": {
                            "type": "integer",
                            "enum": [1, 2],
                            "default": 1,
                        },
                        "bytesize": {
                            "type": "integer",
                            "enum": [7, 8],
                            "default": 8,
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
            ),
        ]
    )


# ============================================================
# MCP TOOL CALL HANDLER
# ============================================================

async def handle_call_tool(
    ctx: ServerRequestContext,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    """
    Execute an MCP tool.

    IMPORTANT:
    The current MCP SDK passes:
        ctx    -> request context
        params -> CallToolRequestParams

    The actual tool name is:
        params.name

    The actual arguments are:
        params.arguments
    """

    name = params.name
    args = params.arguments or {}

    # ========================================================
    # TOOL 1: LIST AVAILABLE SERIAL / USB PORTS
    # ========================================================

    if name == "list_available_serial_ports":

        try:
            ports = serial.tools.list_ports.comports()

            result = []

            for p in ports:
                result.append(
                    {
                        "port": p.device,
                        "description": p.description,
                        "manufacturer": p.manufacturer,
                        "product": p.product,
                        "serial_number": p.serial_number,
                        "vid": p.vid,
                        "pid": p.pid,
                        "interface": p.interface,
                        "hwid": p.hwid,
                    }
                )

            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "success",
                                "port_count": len(result),
                                "ports": result,
                            },
                            indent=2,
                            default=str,
                        ),
                    )
                ]
            )

        except Exception as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "error",
                                "message": "Failed to enumerate serial ports.",
                                "error": str(exc),
                            },
                            indent=2,
                        ),
                    )
                ],
                is_error=True,
            )

    # ========================================================
    # ALL OTHER TOOLS REQUIRE CONNECTION INFORMATION
    # ========================================================

    connection_type = args.get("connection_type")
    endpoint = args.get("endpoint")

    if not connection_type or not endpoint:
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "error",
                            "message": (
                                "Missing required parameters: "
                                "'connection_type' and 'endpoint'."
                            ),
                            "tool": name,
                            "received_arguments": args,
                        },
                        indent=2,
                    ),
                )
            ],
            is_error=True,
        )

    connection_type = str(connection_type).lower().strip()
    endpoint = str(endpoint).strip()

    # Serial parameters
    baudrate = int(args.get("baudrate", 19200))
    parity = str(args.get("parity", "N")).upper()
    stopbits = int(args.get("stopbits", 1))
    bytesize = int(args.get("bytesize", 8))

    # ========================================================
    # TOOL 2: READ PLC STATE
    # ========================================================

    if name == "read_plc_state":

        start_address = int(args.get("start_address", 0))
        count = int(args.get("count", 10))

        if count <= 0:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text="count must be greater than zero.",
                    )
                ],
                is_error=True,
            )

        try:
            client = get_modbus_client(
                connection_type=connection_type,
                endpoint=endpoint,
                baudrate=baudrate,
                parity=parity,
                stopbits=stopbits,
                bytesize=bytesize,
            )

        except Exception as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "error",
                                "message": str(exc),
                            }
                        ),
                    )
                ],
                is_error=True,
            )

        try:
            connected = client.connect()

            if not connected:
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "error",
                                    "message": (
                                        "Failed to connect to PLC."
                                    ),
                                    "connection_type": connection_type,
                                    "endpoint": endpoint,
                                },
                                indent=2,
                            ),
                        )
                    ],
                    is_error=True,
                )

            result = client.read_holding_registers(
                address=start_address,
                count=count,
            )

            if result.isError():
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "error",
                                    "message": "Modbus read failed.",
                                    "details": str(result),
                                },
                                indent=2,
                            ),
                        )
                    ],
                    is_error=True,
                )

            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "success",
                                "connection_type": connection_type,
                                "endpoint": endpoint,
                                "start_address": start_address,
                                "count": count,
                                "values": result.registers,
                            },
                            indent=2,
                        ),
                    )
                ]
            )

        except Exception as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "error",
                                "message": "Exception during Modbus read.",
                                "error": str(exc),
                            },
                            indent=2,
                        ),
                    )
                ],
                is_error=True,
            )

        finally:
            try:
                client.close()
            except Exception:
                pass

    # ========================================================
    # TOOL 3: CREATE PLC BACKUP
    # ========================================================

    if name == "create_plc_backup":

        filepath = args.get(
            "filepath",
            "twido_backup.json",
        )

        try:
            client = get_modbus_client(
                connection_type=connection_type,
                endpoint=endpoint,
                baudrate=baudrate,
                parity=parity,
                stopbits=stopbits,
                bytesize=bytesize,
            )

        except Exception as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "error",
                                "message": str(exc),
                            }
                        ),
                    )
                ],
                is_error=True,
            )

        try:
            if not client.connect():
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text="Failed to connect to PLC.",
                        )
                    ],
                    is_error=True,
                )

            result = client.read_holding_registers(
                address=0,
                count=101,
            )

            if result.isError():
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text="Backup failed during memory read.",
                        )
                    ],
                    is_error=True,
                )

            backup_data = {
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(),
                ),
                "connection_type": connection_type,
                "endpoint": endpoint,
                "start_address": 0,
                "register_count": len(result.registers),
                "holding_registers": result.registers,
            }

            with open(
                filepath,
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    backup_data,
                    f,
                    indent=2,
                )

            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "success",
                                "message": (
                                    f"Backup successfully written "
                                    f"to {filepath}"
                                ),
                                "register_count": len(result.registers),
                            },
                            indent=2,
                        ),
                    )
                ]
            )

        except Exception as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "error",
                                "message": "Backup failed.",
                                "error": str(exc),
                            },
                            indent=2,
                        ),
                    )
                ],
                is_error=True,
            )

        finally:
            try:
                client.close()
            except Exception:
                pass

    # ========================================================
    # TOOL 4: TEST SINGLE OUTPUT
    # ========================================================

    if name == "test_single_output_series":

        output_index = args.get("output_index")
        human_confirmed = args.get(
            "human_confirmed",
            False,
        )

        if output_index is None:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text="output_index is required.",
                    )
                ],
                is_error=True,
            )

        output_index = int(output_index)

        if output_index < 0 or output_index > 15:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text="output_index must be between 0 and 15.",
                    )
                ],
                is_error=True,
            )

        if human_confirmed is not True:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=(
                            "Aborted: human operator must explicitly "
                            "confirm safety before changing PLC outputs."
                        ),
                    )
                ],
                is_error=True,
            )

        try:
            client = get_modbus_client(
                connection_type=connection_type,
                endpoint=endpoint,
                baudrate=baudrate,
                parity=parity,
                stopbits=stopbits,
                bytesize=bytesize,
            )

        except Exception as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=str(exc),
                    )
                ],
                is_error=True,
            )

        try:
            if not client.connect():
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text="Connection failed.",
                        )
                    ],
                    is_error=True,
                )

            # Reset the first 16 coils before testing the requested output.
            for i in range(16):
                client.write_coil(
                    address=i,
                    value=False,
                )

            # Turn selected output ON.
            client.write_coil(
                address=output_index,
                value=True,
            )

            # Keep it ON for 1.5 seconds.
            await asyncio.sleep(1.5)

            # Turn selected output OFF.
            client.write_coil(
                address=output_index,
                value=False,
            )

            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=(
                            f"Pulsed output %Q0.{output_index} "
                            f"for 1.5 seconds and reset it to LOW."
                        ),
                    )
                ]
            )

        except Exception as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "error",
                                "message": "PLC output test failed.",
                                "error": str(exc),
                            },
                            indent=2,
                        ),
                    )
                ],
                is_error=True,
            )

        finally:
            try:
                client.close()
            except Exception:
                pass

    # ========================================================
    # UNKNOWN TOOL
    # ========================================================

    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "status": "error",
                        "message": f"Unknown tool: {name}",
                    }
                ),
            )
        ],
        is_error=True,
    )


# ============================================================
# MCP SERVER
# ============================================================

app = Server(
    "twido-modbus-mcp",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


# ============================================================
# ASYNC SERVER RUNNER
# ============================================================

async def run_server():
    """
    Run the MCP server over stdio.
    """

    async with stdio_server() as (
        read_stream,
        write_stream,
    ):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
