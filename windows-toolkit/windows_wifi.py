"""Temporary, current-user WLAN profiles using Windows Native Wifi.

Never modifies existing profiles, adapter metrics, DNS, or the Ethernet link.
The temporary password stays in memory; Windows stores the profile encrypted.
"""
import asyncio
import ctypes as ct
from ctypes import wintypes as wt
import ipaddress
import socket
import uuid
from xml.sax.saxutils import escape


class GUID(ct.Structure):
    _fields_ = [('data', ct.c_ubyte * 16)]


class Interface(ct.Structure):
    _fields_ = [('guid', GUID), ('description', wt.WCHAR * 256), ('state', wt.DWORD)]


class SSID(ct.Structure):
    _fields_ = [('length', wt.DWORD), ('data', ct.c_ubyte * 32)]


class Association(ct.Structure):
    _fields_ = [('ssid', SSID), ('bss_type', wt.DWORD), ('bssid', ct.c_ubyte * 6),
                ('phy_type', wt.DWORD), ('phy_index', wt.DWORD), ('signal', wt.DWORD),
                ('rx_rate', wt.DWORD), ('tx_rate', wt.DWORD)]


class Security(ct.Structure):
    _fields_ = [('enabled', wt.BOOL), ('onex', wt.BOOL),
                ('auth', wt.DWORD), ('cipher', wt.DWORD)]


class Connection(ct.Structure):
    _fields_ = [('state', wt.DWORD), ('mode', wt.DWORD), ('profile', wt.WCHAR * 256),
                ('association', Association), ('security', Security)]


class Parameters(ct.Structure):
    _fields_ = [('mode', wt.DWORD), ('profile', wt.LPCWSTR), ('ssid', ct.c_void_p),
                ('bssid_list', ct.c_void_p), ('bss_type', wt.DWORD), ('flags', wt.DWORD)]


def profile_xml(name, ssid, password):
    if not 1 <= len(ssid.encode('utf-8')) <= 32:
        raise ValueError('SSID must be 1–32 UTF-8 bytes')
    if not 8 <= len(password) <= 63 or not password.isascii():
        raise ValueError('WPA2 password must be 8–63 ASCII characters')
    return f'''<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{escape(name)}</name>
  <SSIDConfig><SSID><hex>{ssid.encode('utf-8').hex()}</hex><name>{escape(ssid)}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
  <MSM><security><authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
  <sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{escape(password)}</keyMaterial></sharedKey>
  </security></MSM>
</WLANProfile>'''


class TemporaryWifi:
    def __init__(self, interface_description=None):
        self.api = ct.WinDLL('wlanapi.dll')
        handle, ptr, dword = wt.HANDLE, ct.c_void_p, wt.DWORD
        signatures = {
            'WlanOpenHandle': [dword, ptr, ct.POINTER(dword), ct.POINTER(handle)],
            'WlanCloseHandle': [handle, ptr],
            'WlanEnumInterfaces': [handle, ptr, ct.POINTER(ptr)],
            'WlanQueryInterface': [handle, ct.POINTER(GUID), dword, ptr, ct.POINTER(dword), ct.POINTER(ptr), ct.POINTER(dword)],
            'WlanSetProfile': [handle, ct.POINTER(GUID), dword, wt.LPCWSTR, wt.LPCWSTR, wt.BOOL, ptr, ct.POINTER(dword)],
            'WlanConnect': [handle, ct.POINTER(GUID), ct.POINTER(Parameters), ptr],
            'WlanDisconnect': [handle, ct.POINTER(GUID), ptr],
            'WlanDeleteProfile': [handle, ct.POINTER(GUID), wt.LPCWSTR, ptr],
            'WlanScan': [handle, ct.POINTER(GUID), ptr, ptr, ptr],
            'WlanFreeMemory': [ptr],
        }
        for name, args in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = args
            function.restype = None if name == 'WlanFreeMemory' else dword
        self.handle = handle()
        self.profile = None
        self.changed = False
        version = dword()
        self._check(self.api.WlanOpenHandle(2, None, ct.byref(version), ct.byref(self.handle)))
        try:
            memory = ptr()
            self._check(self.api.WlanEnumInterfaces(self.handle, None, ct.byref(memory)))
            try:
                count = ct.cast(memory, ct.POINTER(dword))[0]
                interfaces = (Interface * count).from_address(memory.value + 8)
                choices = [Interface.from_buffer_copy(item) for item in interfaces
                           if interface_description is None or item.description == interface_description]
            finally:
                self.api.WlanFreeMemory(memory)
            if not choices:
                raise RuntimeError('没有可用的无线网卡')
            # Prefer a disconnected adapter so an existing Wi-Fi Internet link stays up.
            self.interface = next((item for item in choices if item.state == 4), choices[0])
            self.original = self.current()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(code):
        if code:
            raise ct.WinError(code)

    def current(self):
        size, kind, memory = wt.DWORD(), wt.DWORD(), ct.c_void_p()
        code = self.api.WlanQueryInterface(self.handle, ct.byref(self.interface.guid), 7,
                                          None, ct.byref(size), ct.byref(memory), ct.byref(kind))
        if code == 5023:  # ERROR_INVALID_STATE: not associated
            return {'connected': False}
        self._check(code)
        try:
            connection = ct.cast(memory, ct.POINTER(Connection)).contents
            association = connection.association
            return {'connected': connection.state == 1, 'profile': connection.profile,
                    'ssid': bytes(association.ssid.data[:association.ssid.length]).decode('utf-8', 'replace'),
                    'signal_percent': association.signal,
                    'rx_mbps': association.rx_rate / 1000, 'tx_mbps': association.tx_rate / 1000}
        finally:
            self.api.WlanFreeMemory(memory)

    def connect_profile(self, name):
        params = Parameters(0, name, None, None, 1, 0)
        self._check(self.api.WlanConnect(self.handle, ct.byref(self.interface.guid), ct.byref(params), None))

    async def join(self, ssid, password, device_ip, timeout=45):
        if not self.changed:
            profile = 'Soundcore-Transfer-' + uuid.uuid4().hex[:12]
            reason = wt.DWORD()
            self._check(self.api.WlanSetProfile(self.handle, ct.byref(self.interface.guid), 2,
                                                profile_xml(profile, ssid, password), None, False,
                                                None, ct.byref(reason)))
            self.profile = profile
            self.changed = True
        self.api.WlanScan(self.handle, ct.byref(self.interface.guid), None, None, None)
        await asyncio.sleep(3)
        loop = asyncio.get_running_loop()
        deadline, retry = loop.time() + timeout, 0
        subnet = ipaddress.ip_network(device_ip + '/24', strict=False)
        while loop.time() < deadline:
            status = self.current()
            if status.get('ssid') == ssid and status.get('connected'):
                # UDP connect selects a route without occupying the device TLS listener.
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    udp.connect((device_ip, 32003))
                    local_ip = udp.getsockname()[0]
                if ipaddress.ip_address(local_ip) in subnet and local_ip != device_ip:
                    return {**status, 'local_ip': local_ip, 'adapter': self.interface.description}
            elif loop.time() >= retry:
                self.connect_profile(self.profile)
                retry = loop.time() + 12
            await asyncio.sleep(1)
        raise TimeoutError('Windows 未能加入录音豆热点或取得同网段地址')

    async def restore(self):
        if not self.changed:
            return {'restored': True, 'changed': False}
        errors = []
        try:
            self._check(self.api.WlanDisconnect(self.handle, ct.byref(self.interface.guid), None))
        except OSError as error:
            errors.append(str(error))
        try:
            self._check(self.api.WlanDeleteProfile(self.handle, ct.byref(self.interface.guid), self.profile, None))
        except OSError as error:
            errors.append(str(error))
        if self.original.get('connected'):
            try:
                self.connect_profile(self.original['profile'])
                for _ in range(20):
                    current = self.current()
                    if current.get('connected') and current.get('ssid') == self.original['ssid']:
                        break
                    await asyncio.sleep(1)
                else:
                    errors.append('原 Wi-Fi 尚未恢复连接')
            except OSError as error:
                errors.append(str(error))
        self.changed = False
        return {'restored': not errors, 'errors': errors, 'current': self.current()}

    def close(self):
        if self.handle:
            self.api.WlanCloseHandle(self.handle, None)
            self.handle = None
