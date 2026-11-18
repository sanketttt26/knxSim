import tempfile
import unittest
import zipfile
from pathlib import Path

from knxsim.instance import Instance
from knxsim.knxproj import load_project, normalize_dpt

NS = 'xmlns="http://knx.org/xml/project/23"'
APP = "M-0083_A-00A0-20-1234"

# ETS 6 layout: <KNX> root, Area/Line/Segment topology, device numbers only,
# integer group addresses, short ids in Links/RefId, base64 serial.
PROJECT_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<KNX {NS} CreatedBy="ETS6">
 <Project Id="P-0501">
  <Installations><Installation Name="">
   <Topology>
    <Area Id="P-0501-0_A-1" Address="1">
     <Line Id="P-0501-0_L-1" Address="1">
      <Segment Id="P-0501-0_S-1" Number="0" MediumTypeRefId="MT-0">
       <DeviceInstance Id="P-0501-0_DI-1" Address="10" Name="Dimmer" SerialNumber="APoAAAAB"
           ProductRefId="M-0083_H-0001_P-0001" Hardware2ProgramRefId="M-0083_H-0001_HP-00A0-20-1234">
        <ComObjectInstanceRefs>
         <ComObjectInstanceRef RefId="O-1_R-1" Links="GA-1 GA-2"/>
         <ComObjectInstanceRef RefId="O-2_R-2" DatapointType="DPST-5-1" Links="GA-3"/>
         <ComObjectInstanceRef RefId="O-3_R-3" Links="GA-5"/>
         <ComObjectInstanceRef RefId="O-4_R-4" DatapointType="DPST-10-1" Links="GA-6"/>
         <ComObjectInstanceRef RefId="O-9_R-9"/>
        </ComObjectInstanceRefs>
       </DeviceInstance>
       <DeviceInstance Id="P-0501-0_DI-3" Name="Not placed" Hardware2ProgramRefId="M-0083_H-0001_HP-00A0-20-1234"/>
      </Segment>
     </Line>
    </Area>
    <Area Id="P-0501-0_A-2" Address="2">
     <Line Id="P-0501-0_L-2" Address="3">
      <DeviceInstance Id="P-0501-0_DI-2" Address="11" Name=""
          ProductRefId="M-0083_H-0001_P-0001" Hardware2ProgramRefId="M-0083_H-0001_HP-00A0-20-1234">
       <ComObjectInstanceRefs>
        <ComObjectInstanceRef RefId="{APP}_O-1_R-1" Text="Temperature" DatapointType="DPST-9-1">
         <Connectors><Send GroupAddressRefId="P-0501-0_GA-4"/></Connectors>
        </ComObjectInstanceRef>
       </ComObjectInstanceRefs>
      </DeviceInstance>
     </Line>
    </Area>
   </Topology>
   <GroupAddresses><GroupRanges><GroupRange Id="P-0501-0_GR-1">
    <GroupAddress Id="P-0501-0_GA-1" Address="2561" Name="Switch"/>
    <GroupAddress Id="P-0501-0_GA-2" Address="2562" Name="Switch central"/>
    <GroupAddress Id="P-0501-0_GA-3" Address="2817" Name="Value"/>
    <GroupAddress Id="P-0501-0_GA-4" Address="3073" Name="Temperature"/>
    <GroupAddress Id="P-0501-0_GA-5" Address="3329" Name="Counter"/>
    <GroupAddress Id="P-0501-0_GA-6" Address="3585" Name="Time"/>
   </GroupRange></GroupRanges></GroupAddresses>
  </Installation></Installations>
 </Project>
</KNX>
"""

INFO_XML = f"""<KNX {NS}><Project Id="P-0501"><ProjectInformation Name="Demo Project"/></Project></KNX>"""

HARDWARE_XML = f"""<KNX {NS}><ManufacturerData><Manufacturer RefId="M-0083"><Hardware>
 <Hardware Id="M-0083_H-0001">
  <Products><Product Id="M-0083_H-0001_P-0001" Text="Universal dimmer" OrderNumber="5WG1 123-4AB1"/></Products>
  <Hardware2Programs><Hardware2Program Id="M-0083_H-0001_HP-00A0-20-1234">
   <ApplicationProgramRef RefId="{APP}"/>
  </Hardware2Program></Hardware2Programs>
 </Hardware>
</Hardware></Manufacturer></ManufacturerData></KNX>"""

APPLICATION_XML = f"""<KNX {NS}><ManufacturerData><Manufacturer RefId="M-0083"><ApplicationPrograms>
 <ApplicationProgram Id="{APP}" ApplicationNumber="160" ApplicationVersion="32" MaskVersion="MV-0705">
  <Static>
   <ComObjectTable>
    <ComObject Id="{APP}_O-1" Number="1" Text="Channel A" FunctionText="Switch" ObjectSize="1 Bit"
        DatapointType="DPST-1-1" ReadFlag="Enabled" WriteFlag="Enabled" TransmitFlag="Enabled"
        UpdateFlag="Disabled" CommunicationFlag="Enabled"/>
    <ComObject Id="{APP}_O-2" Number="2" Text="Value" ObjectSize="1 Byte"
        ReadFlag="Enabled" WriteFlag="Enabled" TransmitFlag="Disabled" UpdateFlag="Disabled"/>
    <ComObject Id="{APP}_O-3" Number="7" Text="Counter" ObjectSize="2 Bytes"
        ReadFlag="Enabled" WriteFlag="Disabled" TransmitFlag="Enabled" UpdateFlag="Disabled"/>
    <ComObject Id="{APP}_O-4" Number="4" Text="Time" ObjectSize="3 Bytes"
        ReadFlag="Enabled" WriteFlag="Enabled" TransmitFlag="Disabled" UpdateFlag="Disabled"/>
   </ComObjectTable>
   <ComObjectRefs>
    <ComObjectRef Id="{APP}_O-1_R-1" RefId="{APP}_O-1"/>
    <ComObjectRef Id="{APP}_O-2_R-2" RefId="{APP}_O-2" WriteFlag="Disabled"/>
    <ComObjectRef Id="{APP}_O-3_R-3" RefId="{APP}_O-3"/>
    <ComObjectRef Id="{APP}_O-4_R-4" RefId="{APP}_O-4"/>
   </ComObjectRefs>
  </Static>
 </ApplicationProgram>
</ApplicationPrograms></Manufacturer></ManufacturerData></KNX>"""


class KnxprojTest(unittest.TestCase):
    def make_project(self, xml: str = PROJECT_XML, protected: bool = False) -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=".knxproj", delete=False)
        tmp.close()
        with zipfile.ZipFile(tmp.name, "w") as archive:
            if protected:
                archive.writestr("P-0501.zip", b"encrypted")
            else:
                archive.writestr("P-0501/0.xml", xml)
                archive.writestr("P-0501/project.xml", INFO_XML)
            archive.writestr("M-0083/Hardware.xml", HARDWARE_XML)
            archive.writestr(f"M-0083/{APP}.xml", APPLICATION_XML)
            archive.writestr("knx_master.xml", "<KNX/>")
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_normalize_dpt(self):
        self.assertEqual(normalize_dpt("DPST-1-1"), "1.001")
        self.assertEqual(normalize_dpt("DPT-9-1"), "9.001")
        self.assertEqual(normalize_dpt("DPST-14-0"), "14.000")
        self.assertEqual(normalize_dpt("DPST-1-1 DPST-1-2"), "1.001")
        self.assertEqual(normalize_dpt("DPT-9"), "9.000")
        self.assertIsNone(normalize_dpt(None))

    def test_load_project(self):
        project = load_project(self.make_project())
        self.assertEqual(project["name"], "Demo Project")
        self.assertEqual([d["ia"] for d in project["devices"]], ["1.1.10", "2.3.11"])  # unplaced device skipped

        dimmer = project["devices"][0]
        self.assertEqual(dimmer["name"], "Dimmer")
        self.assertEqual(dimmer["manufacturer_id"], 0x0083)
        self.assertEqual(dimmer["application_id"], 160)
        self.assertEqual(dimmer["application_version"], 32)
        self.assertEqual(dimmer["mask_version"], 0x0705)
        self.assertEqual(dimmer["order_number"], "5WG1 123-4AB1")
        self.assertEqual(dimmer["serial"], "00fa00000001")  # base64 "APoAAAAB"
        self.assertEqual(dimmer["load_state"], 0)
        self.assertEqual(len(dimmer["channels"]), 4)  # O-9 has no links

        switch, value, counter, time = dimmer["channels"]
        self.assertEqual(switch["number"], 1)
        self.assertEqual(switch["name"], "Channel A - Switch")
        self.assertEqual(switch["dpt"], "1.001")
        self.assertEqual(switch["ga"], {"value": ["1/2/1", "1/2/2"]})  # first link sends
        self.assertEqual(set(switch["flags"]), set("rwt"))
        self.assertEqual(value["dpt"], "5.001")  # instance DPT wins
        self.assertEqual(set(value["flags"]), set("r"))  # ComObjectRef overrides the write flag
        self.assertEqual((counter["number"], counter["dpt"]), (7, "7.001"))  # Number attr, DPT from size
        self.assertEqual(time["dpt"], "10.001")

        sensor = project["devices"][1]  # ETS 4/5 style: full RefId, Connectors
        self.assertEqual(sensor["name"], "Universal dimmer")  # product text when unnamed
        self.assertEqual(sensor["channels"][0]["dpt"], "9.001")
        self.assertEqual(sensor["channels"][0]["ga"], {"value": ["1/4/1"]})

    def test_instances_get_independent_devices(self):
        devices = load_project(self.make_project())["devices"]
        host = type("Host", (), {"emit": lambda self, event: None})()
        one, two = (Instance(host, uid, {"devices": devices}, 3672 + uid) for uid in (1, 2))
        self.assertEqual([d.ia for d in one.devices], [d.ia for d in two.devices])
        one.devices[0].memory_write(0x4000, b"\x01\x02")
        one.devices[0].channels[0].write("value", True)
        self.assertEqual(two.devices[0].memory_read(0x4000, 2), b"\x00\x00")
        self.assertFalse(two.devices[0].channels[0].value)
        self.assertEqual(two.devices[0].load_state, 0)
        self.assertIsNone(two.devices[0].channels[3].value)  # raw 10.001 object loads without a value

    def test_rejects_protected_project(self):
        with self.assertRaisesRegex(ValueError, "password"):
            load_project(self.make_project(protected=True))

    def test_rejects_non_project(self):
        path = self.make_project("<KNX/>")
        with self.assertRaises(ValueError):
            load_project(path)

    def test_rejects_garbage(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".knxproj", delete=False)
        tmp.write(b"not a zip")
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        with self.assertRaises(ValueError):
            load_project(tmp.name)


if __name__ == "__main__":
    unittest.main()
