import json
import time
import asyncio
import serial.tools.list_ports
from pymodbus.client import ModbusTcpClient, ModbusSerialClient

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# Modbus Connection Helper
def get_modbus_client(connection_type: str, endpoint: str, baudrate: int = 19200):
    if connection_type.lower() == "serial":
        return ModbusSerialClient(port=endpoint, baudrate=baudrate, parity='N', stopbits=1, bytesize=8)
    return ModbusTcpClient(host=endpoint, port=502)

# 1. Define list_tools Handler
async def handle_list_tools() -> list[types.Tool]:
    """Expose available MCP tools to the client."""
    return [
        types.Tool(
            name="list_available_serial_ports",
            description="Lists all active COM/serial ports on the host system to locate the PLC adapter.",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="read_plc_state",
            description="Reads holding registers (%MW) directly from the Twido PLC.",
            inputSchema={
                "type": "object",
                "properties": {
                    "connection_type": {"type": "string", "description": "'tcp' or 'serial'"},
                    "endpoint": {"type": "string", "description": "IP address (e.g. '192.168.1.10') or Serial Port (e.g. 'COM3')"},
                    "start_address": {"type": "integer", "default": 0},
                    "count": {"type": "integer", "default": 10}
                },
                "required": ["connection_type", "endpoint"]
            }
        ),
        types.Tool(
            name="create_plc_backup",
            description="Reads memory blocks (%MW0-%MW100) and saves a timestamped JSON snapshot.",
            inputSchema={
                "type": "object",
                "properties": {
                    "connection_type": {"type": "string"},
                    "endpoint": {"type": "string"},
                    "filepath": {"type": "string", "default": "twido_backup.json"}
                },
                "required": ["connection_type", "endpoint"]
            }
        ),
        types.Tool(
            name="test_single_output_series",
            description="Toggles a single PLC output (%Q0.X) sequentially for I/O mapping. Requires human confirmation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "connection_type": {"type": "string"},
                    "endpoint": {"type": "string"},
                    "output_index": {"type": "integer"},
                    "human_confirmed": {"type": "boolean"}
                },
                "required": ["connection_type", "endpoint", "output_index", "human_confirmed"]
            }
        )
    ]

# 2. Define call_tool Handler
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Execute tools called by the client."""
    if name == "list_available_serial_ports":
        ports = serial.tools.list_ports.comports()
        if not ports:
            return [types.TextContent(type="text", text="No active serial/USB ports found on host.")]
        res = [{"port": p.device, "description": p.description} for p in ports]
        return [types.TextContent(type="text", text=json.dumps(res, indent=2))]

    connection_type = arguments.get("connection_type")
    endpoint = arguments.get("endpoint")

    if name == "read_plc_state":
        start_address = arguments.get("start_address", 0)
        count = arguments.get("count", 10)
        client = get_modbus_client(connection_type, endpoint)
        if not client.connect():
            return [types.TextContent(type="text", text=json.dumps({"status": "error", "message": "Failed to connect to PLC"}))]
        
        res = client.read_holding_registers(start_address, count)
        client.close()
        
        if res.isError():
            return [types.TextContent(type="text", text=json.dumps({"status": "error", "message": "Modbus read failed"}))]
        return [types.TextContent(type="text", text=json.dumps({"status": "success", "values": res.registers}))]

    elif name == "create_plc_backup":
        filepath = arguments.get("filepath", "twido_backup.json")
        client = get_modbus_client(connection_type, endpoint)
        if not client.connect():
            return [types.TextContent(type="text", text="Failed to connect to PLC.")]
        
        res = client.read_holding_registers(0, 100)
        client.close()
        if res.isError():
            return [types.TextContent(type="text", text="Backup failed during memory read.")]

        backup_data = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "holding_registers": res.registers
        }
        with open(filepath, "w") as f:
            json.dump(backup_data, f, indent=2)
        return [types.TextContent(type="text", text=f"Backup successfully written to {filepath}")]

    elif name == "test_single_output_series":
        output_index = arguments.get("output_index")
        human_confirmed = arguments.get("human_confirmed", False)
        
        if not human_confirmed:
            return [types.TextContent(type="text", text="Aborted: Human operator must confirm safety.")]

        client = get_modbus_client(connection_type, endpoint)
        if not client.connect():
            return [types.TextContent(type="text", text="Connection failed.")]

        try:
            for i in range(16):
                client.write_coil(i, False)
            client.write_coil(output_index, True)
            time.sleep(1.5)
            client.write_coil(output_index, False)
            return [types.TextContent(type="text", text=f"Pulsed output %Q0.{output_index} for 1.5s and reset to LOW.")]
        finally:
            client.close()

    raise ValueError(f"Unknown tool: {name}")

# 3. Instantiate Server
app = Server(
    "twido-modbus-mcp",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool
)

# 4. Async Execution Wrapper
async def run_server():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )

# 5. Synchronous entry point called by script CLI
def main():
    asyncio.run(run_server())

if __name__ == "__main__":
    main()