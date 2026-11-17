# KNXSim: multi-instance KNX installation simulator

KNXSim simulates complete KNX installations (actuators, push-buttons, sensors,
thermostats) behind a standard **KNXnet/IP tunnelling interface**. That means ETS,
Home Assistant (xknx), openHAB, visualisation servers and any other KNX software
can connect to it just like a real KNX IP interface.

Its main feature is the one KNX Virtual (`KV.exe`) lacks: you can run **any number of
independent installations on one IP address**, in one process or in several.

```
python -m knxsim -n 3
KNXSim running - 3 instance(s) on 192.168.0.107:
  [1] KNX Sim 1                UDP 3672  interface 1.1.250
  [2] KNX Sim 2                UDP 3673  interface 1.1.250
  [3] KNX Sim 3                UDP 3674  interface 1.1.250
  discovery on UDP 3671: on
  UI: http://localhost:8080
```

It needs only Python 3 (standard library, no `pip install`) and runs on Windows,
Linux and macOS.

## Why KV.exe runs only once per machine

`KV.exe` v2.6.1 is an obfuscated .NET 4 WinForms app. Its metadata and static data show this:

* One `UdpClient` is created as `new UdpClient(new IPEndPoint(IPAddress.Any, 3671))`
  and joins multicast `224.0.23.12`. A second socket is bound with `Socket.Bind` to
  the machine's IP (found via `Dns.GetHostEntry`), also on port **3671**.
* `SetSocketOption`/`ExclusiveAddressUse` are never referenced, so the socket is
  exclusive. A second copy fails to bind 3671.
* Its frame templates hard-code the identity: serial `00FA:00000001`, one MAC, name
  "KNX Virtual", control endpoint port 3671. Even if two copies could bind, ETS would
  see two identical interfaces at the same `IP:3671`.
* Everything else is a normal KNXnet/IP server: search/description, tunnelling
  (`TUNNELLING_REQUEST`/`ACK`), device-management connections, and many pre-built
  management responses (PropertyValue, DeviceDescriptor mask `07B0`, Authorize,
  Restart, serial-number addressing, Data Secure).

## How KNXSim fits many installations on one IP

| | KV.exe | KNXSim |
|---|---|---|
| Discovery (UDP 3671, multicast 224.0.23.12) | exclusive socket | shared socket (`SO_REUSEADDR`/`SO_REUSEPORT`) so every process can answer searches |
| Control/data endpoint | always `IP:3671` | one UDP port per instance (3672, 3673, ...), advertised in each `SEARCH_RESPONSE` |
| Identity | fixed serial/MAC | unique serial + MAC per instance (derived from its port) |
| Bus | one | one isolated virtual bus per instance |

ETS sends one search and gets one answer per instance, e.g. `KNX Sim 1 (192.168.0.107:3672)`
and `KNX Sim 2 (192.168.0.107:3673)`. Telegrams sent to one instance never reach another.
If you start a second KNXSim process, its instances pick the next free ports
automatically and show up in the same search.

## Quick start

```bash
cd KNXSim
python -m knxsim                 # 1 installation
python -m knxsim -n 4            # 4 independent installations
python -m knxsim -c examples/training-room.json
```

Open the UI at http://localhost:8080. It has one tab per installation, with clickable
devices, PROG buttons and a live group monitor. You can add or remove installations
at runtime.

| Option | Meaning |
|---|---|
| `-n N` | N instances with the default installation |
| `-c FILE` | load instances from a JSON file (see below) |
| `--ip IP` | IP to advertise / join multicast on (default: picked per client automatically) |
| `--base-port P` | first instance port (default 3672) |
| `--web-port P` / `--web-host H` | UI address (default `127.0.0.1:8080`; use `--web-host 0.0.0.0` to open it to the LAN) |
| `--no-web`, `--no-discovery`, `-v` | headless / no search answers / debug logging |

**Windows:** allow `python.exe` through the firewall for private networks the first
time Windows asks. Otherwise ETS cannot reach the UDP ports.

### Connecting ETS

*Bus → Connections → Interfaces*: each instance appears under **Discovered
interfaces** with its own port. Or add a manual connection: type *IP Tunnelling*,
IP = the address KNXSim prints, port = the instance port.

What works from ETS:
* Group monitor, sending/reading group values (ETS "Bus monitor" mode needs a busmonitor tunnel, which is not supported)
* Programming individual addresses: press **PROG** on a device in the UI, then use
  ETS "Program individual address". ETS's address check, IA write, device-descriptor
  verify and restart all work.
* Device info (device descriptor, serial number, manufacturer property)

### Other software

Point any KNXnet/IP *tunnelling* client at `IP:port`. Example for xknx / Home Assistant:
gateway `192.168.0.107`, port `3673`, connection type *tunnelling (UDP)*.

## Configuration file

```json
{
  "instances": [
    {"name": "Trainee 1"},
    {"name": "Demo", "port": 3680, "interface_ia": "2.1.240", "tunnels": 2,
     "devices": [
       {"ia": "2.1.1", "name": "Actuator", "channels": [
         {"type": "switch", "name": "Lamp", "ga": {"switch": "5/0/1", "status": "5/1/1"}}
       ]}
     ]}
  ]
}
```

* An instance without `devices` gets the default installation
  (`knxsim/default_installation.json`, 7 devices). Copy that file to start your own.
* `port` is optional (next free port from `--base-port`). `interface_ia` defaults to
  `1.1.250`. The `tunnels` addresses (default 4) follow it: `1.1.251`–`1.1.254`.
* A group address value can be a list: the first address sends and answers reads,
  the others are listen-only (e.g. a central "all off" address).

| Channel `type` | Objects (DPT) | Options |
|---|---|---|
| `switch` | `switch` (1.001 W), `status` (1.001 R) | `value` |
| `dimmer` | `switch` (1.001), `dim` (3.007), `value` (5.001), `switch_status`, `value_status` | `ramp_seconds` |
| `blind` | `move` (1.008), `stop` (1.007), `position` (5.001), `position_status` | `travel_seconds` |
| `button` | `switch` (1.001, sends), `status` (1.001, listens) | `mode`: toggle / on / off |
| `sensor` | `value` (any of 1.x, 5.x, 7.x, 9.x, 14.x) | `dpt`, `unit`, `value`, `min`, `max`, `step` |
| `thermostat` | `temperature`, `setpoint` (9.001), `heating` (5.001 valve %) | `temperature`, `setpoint`, `gain` |

Status objects send on change and answer `GroupValueRead`.

## Limitations

* **No ETS application download.** ETS can download parameters/group links only into
  devices that exist in its product catalogue (`.knxprod`). KV's devices come from
  KNX Association's own catalogue entries. KNXSim's devices are configured through the
  JSON file instead: give them the group addresses of your ETS project.
* Tunnelling v1 over UDP only (no TCP, KNX IP Secure, Data Secure or routing multicast).
* Port 3671 is reserved for discovery, so instances live on 3672+. Clients that cannot
  set a port can't reach them; nearly all can (ETS, xknx, openHAB, Calimero ...).
* Unicast search requests sent to `IP:3671` reach only one process when several
  KNXSim processes run. Multicast discovery, which ETS uses, reaches all of them.

## Tests

```bash
python -m unittest discover -s tests -t .        # end-to-end tests need: pip install xknx
```

The end-to-end tests use xknx as an independent KNX client. They check two instances
on one IP with isolated buses, group write/read/status, push-button → actuator,
ETS-style individual-address programming, tunnel address pool, the web API, and
discovery of two separate KNXSim processes by one multicast search.
