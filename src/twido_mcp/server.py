import json
import time
from mcp.server.fastmcp import FastMCP
from pymodbus.client import ModbusTcpClient, ModbusSerialClient
import serial.tools.list_ports


mcp = FastMCP("Twido-Modbus-MCP")

# Modbus Connection Helper
def get_modbus_client(connection_type: str, host_or_port: str, baudrate: int = 19200):
    if connection_type.lower() == "serial":
        return ModbusSerialClient(port=host_or_port, baudrate=baudrate, parity='N', stopbits=1, bytesize=8)
    return ModbusTcpClient(host=host_or_port, port=502)

@mcp.tool()
def list_available_serial_ports() -> str:
    """Lists all active COM/serial ports on the host system to locate the PLC adapter."""
    ports = serial.tools.list_ports.comports()
    if not ports:
        return "No active serial/USB ports found on the host system."

    result = [{"port": p.device, "description": p.description} for p in ports]
    return json.dumps(result)

@mcp.tool()
def read_plc_state(connection_type: str, endpoint: str, start_address: int = 0, count: int = 10) -> str:
    """Reads registers (%MW) or coils (%Q/%I) directly from the Twido PLC.
    connection_type: 'tcp' or 'serial'
    endpoint: IP address (e.g. '192.168.1.10') or Serial Port (e.g. 'COM3' or '/dev/ttyUSB0')
    """
    client = get_modbus_client(connection_type, endpoint)
    if not client.connect():
        return json.dumps({"status": "error", "message": "Failed to connect to PLC"})

    res = client.read_holding_registers(start_address, count)
    client.close()

    if res.isError():
        return json.dumps({"status": "error", "message": "Modbus read operation failed"})
    return json.dumps({"status": "success", "start_address": start_address, "values": res.registers})

@mcp.tool()
def create_plc_backup(connection_type: str, endpoint: str, filepath: str = "twido_backup.json") -> str:
    """Reads memory blocks (%MW0-%MW100) and saves a timestamped JSON snapshot."""
    client = get_modbus_client(connection_type, endpoint)
    if not client.connect():
        return "Failed to establish PLC connection."

    res = client.read_holding_registers(0, 100)
    client.close()

    if res.isError():
        return "Backup failed during memory read."

    backup_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "holding_registers": res.registers
    }
    with open(filepath, "w") as f:
        json.dump(backup_data, f, indent=2)

    return f"Backup successfully stored in {filepath}"

@mcp.tool()
def test_single_output_series(connection_type: str, endpoint: str, output_index: int, human_confirmed: bool) -> str:
    """Toggles a single PLC output (%Q0.X) sequentially for I/O mapping.
    SAFETY: Sets all outputs to LOW first, then pulses ONLY the specified output.
    Requires human_confirmed=True.
    """
    if not human_confirmed:
        return "Aborted: Human operator must confirm physical safety before toggling hardware."

    client = get_modbus_client(connection_type, endpoint)
    if not client.connect():
        return "Connection failed."

    try:
        # Step 1: Force reset all digital outputs (%Q) to safe LOW state
        for i in range(16):
            client.write_coil(i, False)

        # Step 2: Pulse the requested output for physical check
        client.write_coil(output_index, True)
        time.sleep(1.5)  # Output active window for physical indicator
        client.write_coil(output_index, False)

        return f"Output %Q0.{output_index} was pulsed HIGH for 1.5s and reset to LOW. Verify physical element."
    finally:
        client.close()

def main():
    mcp.run()

if __name__ == "__main__":
    main()