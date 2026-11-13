"""Exercise retry/pause/exit races without recording or connecting hardware."""
import asyncio
from pathlib import Path
import unittest
from soundcore_microphone import BridgeService, CableOutput, InstanceControl


class DummyOutput:
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def push(self,pcm): pass


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    def test_rdp_fallback_only_opens_virtual_cable_kernel_endpoint(self):
        from unittest.mock import patch, Mock
        devices=[{'name':'Speakers','max_output_channels':2,'hostapi':0},
                 {'name':'Output (VB-Audio Point)','max_output_channels':16,'hostapi':1}]
        with patch('soundcore_microphone.sd.query_devices',return_value=devices), \
             patch('soundcore_microphone.sd.query_hostapis',side_effect=lambda i:{'name':['Windows WASAPI','Windows WDM-KS'][i]}), \
             patch('soundcore_microphone.sd.RawOutputStream',return_value=Mock()) as stream:
            output=CableOutput()
            self.assertEqual(output.backend,'Windows WDM-KS')
            self.assertEqual(stream.call_args.kwargs['device'],1)
            self.assertIsNone(stream.call_args.kwargs['extra_settings'])
            self.assertEqual(stream.call_args.kwargs['samplerate'],16000)

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
