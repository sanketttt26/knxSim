"""End-to-end tests against xknx, a third-party KNXnet/IP client (pip install xknx)."""
import asyncio
import json
import os
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

try:
    from xknx import XKNX
    from xknx.dpt import DPTBinary
    from xknx.io import ConnectionConfig, ConnectionType, GatewayScanner
    from xknx.management.procedures import nm_individual_address_write
    from xknx.telegram import GroupAddress, IndividualAddress, Telegram, apci
except ImportError:  # pragma: no cover
    XKNX = None

from knxsim.host import Host
from knxsim.web import WebUI

BASE = 23672  # stay clear of a simulator the developer may have running
ROOT = Path(__file__).resolve().parents[1]


def client(port: int) -> "XKNX":
    return XKNX(connection_config=ConnectionConfig(
        connection_type=ConnectionType.TUNNELING, gateway_ip="127.0.0.1", gateway_port=port,
        local_ip="127.0.0.1"))


@unittest.skipIf(XKNX is None, "xknx not installed")
class InteropTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = Host(ip="127.0.0.1", base_port=BASE)
        await self.host.start(discovery=False)
        self.a = await self.host.add_instance({"name": "House A"})
        self.b = await self.host.add_instance({"name": "House B"})
        self.clients = []

    async def asyncTearDown(self):
        for c in self.clients:
            await c.stop()
        self.host.stop()
        await asyncio.sleep(0.05)

    async def connect(self, inst):
        c = client(inst.port)
        received = []
        c.telegram_queue.register_telegram_received_cb(received.append)
        await c.start()
        self.clients.append(c)
        return c, received

    async def wait_for(self, received, predicate, timeout=3.0):
        for _ in range(int(timeout / 0.05)):
            hits = [t for t in received if predicate(t)]
            if hits:
                return hits[0]
            await asyncio.sleep(0.05)
        self.fail(f"telegram not received; got {received}")

    async def test_two_instances_share_ip_but_not_bus(self):
        ca, rx_a = await self.connect(self.a)
        cb, rx_b = await self.connect(self.b)
        self.assertEqual({self.a.port, self.b.port}, {BASE, BASE + 1})

        await ca.telegrams.put(Telegram(destination_address=GroupAddress("1/0/1"),
                                        payload=apci.GroupValueWrite(DPTBinary(1))))
        status = await self.wait_for(rx_a, lambda t: t.destination_address == GroupAddress("1/1/1"))
        self.assertEqual(status.source_address, IndividualAddress("1.1.1"))
        self.assertEqual(status.payload.value.value, 1)
        self.assertTrue(self.a.devices[0].channels[0].on)

        await asyncio.sleep(0.3)
        self.assertFalse(self.b.devices[0].channels[0].on, "instance B must not see A's bus")
        self.assertEqual(rx_b, [])

    async def test_group_read_response(self):
        ca, rx = await self.connect(self.a)
        self.a.action(4, 0, "temperature", 22.5)  # room thermostat measured value
        await ca.telegrams.put(Telegram(destination_address=GroupAddress("3/0/1"),
                                        payload=apci.GroupValueRead()))
        resp = await self.wait_for(rx, lambda t: isinstance(t.payload, apci.GroupValueResponse))
        self.assertEqual(resp.source_address, IndividualAddress("1.1.5"))
        self.assertEqual(bytes(resp.payload.value.value), bytes.fromhex("0c65"))  # 22.5 as DPT 9

    async def test_push_button_drives_actuator(self):
        ca, rx = await self.connect(self.a)
        self.a.action(3, 0, "press", None)  # button 1.1.4 ch0 -> 1/0/1
        await self.wait_for(rx, lambda t: t.source_address == IndividualAddress("1.1.4"))
        await self.wait_for(rx, lambda t: t.destination_address == GroupAddress("1/1/1"))
        self.assertTrue(self.a.devices[0].channels[0].on)
        self.a.action(3, 3, "press", None)  # "All off" via central 1/0/0
        await asyncio.sleep(0.2)
        self.assertFalse(self.a.devices[0].channels[0].on)

    async def test_ets_style_individual_address_programming(self):
        ca, _ = await self.connect(self.a)
        self.a.action(1, None, "prog", True)  # press the programming button of 1.1.2
        await nm_individual_address_write(ca, IndividualAddress("1.1.20"))
        self.assertEqual(self.a.snapshot()["devices"][1]["ia"], "1.1.20")
        async with ca.management.connection(IndividualAddress("1.1.20")) as conn:
            resp = await conn.request(payload=apci.DeviceDescriptorRead(0))
            self.assertEqual(resp.payload.value, 0x07B0, resp)
        # nothing changed on the other installation
        self.assertEqual(self.b.snapshot()["devices"][1]["ia"], "1.1.2")

    async def test_tunnel_slots(self):
        clients = [await self.connect(self.a) for _ in range(4)]
        ias = {str(c.current_address) for c, _ in clients}
        self.assertEqual(ias, {"1.1.251", "1.1.252", "1.1.253", "1.1.254"})
        self.assertEqual(len(self.a.connections), 4)

    async def test_web_api(self):
        web = WebUI(self.host, "127.0.0.1", BASE + 100)
        await web.start()
        try:
            def call(method, path, body=None):
                req = urllib.request.Request(f"http://127.0.0.1:{web.port}{path}", method=method,
                                             data=json.dumps(body).encode() if body is not None else None)
                with urllib.request.urlopen(req, timeout=3) as r:
                    return json.loads(r.read())
            state = await asyncio.to_thread(call, "GET", "/api/state")
            self.assertEqual([i["name"] for i in state["instances"]], ["House A", "House B"])
            await asyncio.to_thread(call, "POST", f"/api/instances/{self.a.id}/action",
                                    {"device": 0, "channel": 1, "action": "toggle"})
            self.assertTrue(self.a.devices[0].channels[1].on)
            new = await asyncio.to_thread(call, "POST", "/api/instances", {"name": "House C"})
            self.assertEqual(self.host.instances[new["id"]].port, BASE + 2)
            await asyncio.to_thread(call, "DELETE", f"/api/instances/{new['id']}")
            self.assertNotIn(new["id"], self.host.instances)
        finally:
            web.close()


@unittest.skipIf(XKNX is None, "xknx not installed")
class DiscoveryTest(unittest.IsolatedAsyncioTestCase):
    """Two separate simulator processes on the same IP, found by one multicast search."""

    async def test_two_processes_discovered(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        procs = [subprocess.Popen([sys.executable, "-m", "knxsim", "-n", "2", "--no-web",
                                   "--base-port", str(BASE + 200)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                 for _ in range(2)]
        try:
            await asyncio.sleep(1.5)
            xknx = XKNX()
            found = await GatewayScanner(xknx, timeout_in_seconds=2).scan()
            ours = sorted(g.port for g in found if g.name.startswith("KNX Sim"))
            outputs = []
            for p in procs:
                p.terminate()
                outputs.append(p.communicate(timeout=5)[0])
            self.assertEqual(ours, [BASE + 200, BASE + 201, BASE + 202, BASE + 203], "\n".join(outputs))
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()


if __name__ == "__main__":
    unittest.main()
