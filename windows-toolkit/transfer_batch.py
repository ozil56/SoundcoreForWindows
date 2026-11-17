"""One temporary hotspot per download batch, with verified BLE fallback."""
from pathlib import Path
import time

from recorder_files import RecordingStarted
from tool_settings import diagnostics_folder, write_json
from wifi_transfer import WifiTransfer


class TransferRecoveryError(RuntimeError):
    pass


class DownloadBatch:
    def __init__(self, session, root, settings, notice=lambda _: None):
        self.session = session
        self.root = Path(root)
        self.notice = notice
        self.use_wifi = settings.get('wifi_fast_transfer', True)
        self.link = None
        self.attempted = False
        self.report = {'wifi_enabled': self.use_wifi, 'exports': []}

    async def __aenter__(self):
        return self

    def check_idle(self):
        self.session.check_idle()

    async def _close_wifi(self, *exc):
        link, self.link = self.link, None
        if link:
            self.notice('正在关闭热点并恢复蓝牙连接…')
            await link.__aexit__(*exc)
        cleanup = self.report.get('wifi_cleanup', {})
        if self.report.get('recovery_errors') or cleanup.get('restored') is False:
            raise TransferRecoveryError('热点收尾或网络恢复失败，已保留本地文件；请重连后再试：' +
                                        '; '.join(self.report.get('recovery_errors', []) + cleanup.get('errors', [])))

    async def export(self, recording, folder, progress=lambda *_: None):
        self.check_idle()
        if self.use_wifi and not self.attempted:
            self.attempted = True
            candidate = WifiTransfer(self.session, self.report, self.notice)
            try:
                self.link = await candidate.__aenter__()
            except RecordingStarted:
                raise
            except Exception as error:
                # __aenter__ has already restored any partially opened Wi-Fi link.
                await self._close_wifi(None, None, None)
                self.check_idle()  # Preserve schedule/cancellation/recording guards.
                self.report['fallback_reason'] = f'{type(error).__name__}: {error}'
                self.notice('Wi-Fi 暂不可用，改用蓝牙下载：' + str(error))
        if self.link:
            try:
                result = await self.link.export(recording, folder, progress)
            except RecordingStarted:
                raise
            except Exception as error:
                await self._close_wifi(type(error), error, error.__traceback__)
                self.check_idle()
                self.report['fallback_reason'] = f'{type(error).__name__}: {error}'
                self.notice('Wi-Fi 下载未完成，已恢复蓝牙，重新下载当前文件…')
            else:
                self.report['exports'].append({'file_id':recording.file_id, 'transport':'wifi',
                                               'seconds':result.get('transfer_seconds')})
                return result
        self.notice('正在通过蓝牙下载录音…')
        started = time.monotonic()
        result = await self.session.export(recording, folder, progress)
        result.update(transport='ble', transfer_seconds=round(time.monotonic()-started, 3))
        self.report['exports'].append({'file_id':recording.file_id, 'transport':'ble',
                                       'seconds':result['transfer_seconds']})
        return result

    async def __aexit__(self, *exc):
        try:
            await self._close_wifi(*exc)
        finally:
            try:
                write_json(diagnostics_folder(self.root)/'last-transfer.json', self.report)
            except OSError:
                pass  # A logging failure must not invalidate an already saved WAV.
