"""Exercise retry/pause/exit races without recording or connecting hardware."""
import asyncio
from pathlib import Path
import unittest
from soundcore_microphone import BridgeService, CableOutput, OptionalCableOutput, InstanceControl, is_remote_session


class DummyOutput:
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def push(self,pcm): pass


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    def test_audio_device_refresh_keeps_ble_thread_mta_in_real_windows_runtime(self):
        # Isolate PortAudio's global cache from other tests. No audio stream or
        # Bluetooth device is opened; this exercises the real COM regression.
        import subprocess,sys,textwrap
        code=textwrap.dedent('''
            import ctypes,threading
            import sounddevice as sd
            from soundcore_microphone import BluetoothThreadCOM
            errors=[]
            def check():
                try:
                    with BluetoothThreadCOM():
                        for _ in range(2):
                            sd._terminate(); sd._initialize()
                            apartment=ctypes.c_int(); qualifier=ctypes.c_int()
                            hr=ctypes.windll.ole32.CoGetApartmentType(ctypes.byref(apartment),ctypes.byref(qualifier))
                            assert hr==0 and apartment.value==1, (hr,apartment.value)
                except BaseException as e: errors.append(repr(e))
            worker=threading.Thread(target=check); worker.start(); worker.join(20)
            assert not worker.is_alive(), 'Audio refresh timed out'
            assert not errors, errors
        ''')
        result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).parent,
                              capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_missing_wasapi_never_opens_kernel_endpoint(self):
        from unittest.mock import patch, Mock
        devices=[{'name':'Speakers','max_output_channels':2,'hostapi':0},
                 {'name':'Output (VB-Audio Point)','max_output_channels':16,'hostapi':1}]
        with patch('soundcore_microphone.sd.query_devices',return_value=devices), \
             patch('soundcore_microphone.sd.query_hostapis',side_effect=lambda i:{'name':['Windows WASAPI','Windows WDM-KS'][i]}), \
             patch('soundcore_microphone.sd.RawOutputStream',return_value=Mock()) as stream:
            with self.assertRaisesRegex(RuntimeError,'CABLE Input'): CableOutput()
            stream.assert_not_called()

    def test_only_standard_cable_input_is_used(self):
        from unittest.mock import patch, Mock
        devices=[{'name':'CABLE In 16ch (VB-Audio Virtual Cable)','max_output_channels':2,'hostapi':0},
                 {'name':'Output (VB-Audio Point)','max_output_channels':16,'hostapi':1},
                 {'name':'CABLE Input (VB-Audio Virtual Cable)','max_output_channels':2,'hostapi':0}]
        with patch('soundcore_microphone.sd.query_devices',return_value=devices), \
             patch('soundcore_microphone.sd.query_hostapis',side_effect=lambda i:{'name':['Windows WASAPI','Windows WDM-KS'][i]}), \
             patch('soundcore_microphone.sd.RawOutputStream',return_value=Mock()) as stream:
            output=CableOutput()
            self.assertEqual(output.backend,'Windows WASAPI')
            self.assertEqual(stream.call_args.kwargs['device'],2)
            self.assertIsNotNone(stream.call_args.kwargs['extra_settings'])

    def test_remote_session_detection_reads_live_protocol_and_frees_buffer(self):
        import ctypes
        from unittest.mock import patch, Mock
        for protocol in (0,2):
            value=ctypes.c_ushort(protocol)
            api=Mock()
            def query(server,session,kind,buffer,size):
                self.assertEqual(session,0xffffffff); self.assertEqual(kind,16)
                buffer._obj.value=ctypes.addressof(value)
                size._obj.value=ctypes.sizeof(value)
                return True
            api.WTSQuerySessionInformationW.side_effect=query
            with patch('soundcore_microphone.ctypes.WinDLL',return_value=api):
                self.assertEqual(is_remote_session(),protocol!=0)
            api.WTSFreeMemory.assert_called_once()

    def test_rdp_suspends_audio_and_console_reopens_same_wasapi_with_fresh_devices(self):
        from unittest.mock import patch, MagicMock
        output=OptionalCableOutput()
        with patch('soundcore_microphone.is_remote_session',return_value=True) as remote, \
             patch('soundcore_microphone.sd._terminate') as terminate, \
             patch('soundcore_microphone.sd._initialize') as initialize, \
             patch('soundcore_microphone.CableOutput') as factory:
            first,second=MagicMock(),MagicMock()
            first.stream.active=second.stream.active=True
            factory.side_effect=[first,second]
            output.__enter__(); output.push(b'audio')
            factory.assert_not_called(); initialize.assert_not_called()
            self.assertTrue(output.remote_session)
            remote.return_value=False; output.poll(); output.push(b'local')
            first.push.assert_called_once_with(b'local')
            output.poll()
            self.assertEqual(factory.call_count,1)
            remote.return_value=True; output.poll(); output.push(b'remote')
            first.__exit__.assert_called_once()
            self.assertIsNone(output.output)
            remote.return_value=False; output.poll()
            self.assertIs(output.output,second)
            self.assertEqual(initialize.call_count,2); self.assertEqual(terminate.call_count,2)
            self.assertFalse(output.remote_session)
            output.__exit__(); second.__exit__.assert_called_once()

    def test_inactive_wasapi_stream_is_closed_before_refresh(self):
        from unittest.mock import patch, MagicMock
        output=OptionalCableOutput(); old=MagicMock(); old.stream.active=False
        output.output=old
        with patch('soundcore_microphone.is_remote_session',return_value=False), \
             patch('soundcore_microphone.sd._terminate') as terminate, \
             patch('soundcore_microphone.sd._initialize'), \
             patch('soundcore_microphone.CableOutput') as factory:
            def refresh(): old.__exit__.assert_called_once()
            terminate.side_effect=refresh
            output.poll()
            self.assertIs(output.output,factory.return_value)
            output.__exit__()

    async def test_audio_session_monitor_keeps_device_connection_and_idle_sync(self):
        from unittest.mock import Mock
        output=DummyOutput(); output.poll=Mock()
        receiver=Mock()
        receiver.frames_ok=receiver.samples=0
        async def run(**kwargs): await asyncio.Event().wait()
        receiver.run=run
        pipeline=Mock()
        service=BridgeService(Path(__file__).parent,lambda _:None,
            lambda *args,**kwargs:receiver,lambda:output,pipeline=pipeline)
        task=asyncio.create_task(service.attempt())
        try:
            async with asyncio.timeout(3):
                while not output.poll.called: await asyncio.sleep(.02)
            self.assertIs(service.receiver,receiver)
            self.assertIs(receiver.idle_callback,pipeline.when_idle)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertIsNone(service.output)

    async def test_missing_device_retry_pause_resume_and_exit(self):
        attempts=[]
        closed=[]
        class FakeReceiver:
            def __init__(self,*args,**kwargs):
                self.frames_ok=0
                self.samples=0
                self.number=len(attempts)
                attempts.append(self)
            async def run(self,**kwargs):
                if self.number==0:
                    raise RuntimeError('device temporarily absent')
                await asyncio.Event().wait()
            def close(self): closed.append(self.number)

        service=BridgeService(Path(__file__).parent,lambda _:None,
                              FakeReceiver,DummyOutput,retry_seconds=.02)
        runner=asyncio.create_task(service.run())
        async def until(condition):
            async with asyncio.timeout(2):
                while not condition(): await asyncio.sleep(.01)
        try:
            await until(lambda:len(attempts)==2)
            service.command('pause')
            await until(lambda:len(closed)==2)
            await asyncio.sleep(.08)
            self.assertEqual(len(attempts),2,'pause must suppress automatic retries')
            service.command('resume')
            await until(lambda:len(attempts)==3)
            service.command('reconnect')
            await until(lambda:len(attempts)==4)
            service.command('quit')
            await asyncio.wait_for(runner,2)
            self.assertEqual(closed,[0,1,2,3])
            self.assertEqual(len(attempts),4,'exit must not start a new connection')
        finally:
            if not runner.done():
                service.command('quit')
                await asyncio.wait_for(runner,2)

    def test_single_instance_and_local_commands(self):
        first=InstanceControl(Path(__file__))
        second=InstanceControl(Path(__file__))
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            self.assertTrue(second.send('show'))
            self.assertEqual(first.pending(),['show'])
            self.assertEqual(first.pending(),[])
        finally:
            first.close(); second.close()

    def test_audio_handle_closed_when_device_is_not_ready(self):
        from unittest.mock import Mock
        output=CableOutput.__new__(CableOutput)
        output.stream=Mock()
        output.stream.start.side_effect=RuntimeError('audio driver not ready')
        with self.assertRaises(RuntimeError): output.__enter__()
        output.stream.close.assert_called_once()
        output.stream.reset_mock()
        output.stream.stop.side_effect=RuntimeError('device unplugged')
        with self.assertRaises(RuntimeError): output.__exit__()
        output.stream.close.assert_called_once()


if __name__=='__main__': unittest.main()
