import asyncio
import os.path
import random
import ssl
import subprocess
import tempfile
from typing import Awaitable, Callable
import unittest

from .backup_archive import make_validator_data_backup
from .config import Config
from .rpc.auth import gen_user_key
from .rpc.client import RpcClient
from .ssh import SSHConnInfo
from .key_ops import KeyDescriptor
from .supervisor import ValidatorSupervisor
from .validators import ValidatorRelease

ETH2_NETWORK = 'pyrmont'
PASSWORD = 'password123'
TEST_MNEMONIC = b'clog dust clip zone cute decrease correct quantum forget climb buffalo ' \
    b'girl plunge fuel together warfare space cost memory able evolve rebel orient check'
CONTAINER_NAME = 'TEST_validator-supervisor_supervisor'


class SupervisorRemoteControlIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Sometimes the container will be created and not removed if a test case run is interrupted
        subprocess.run(['docker', 'rm', CONTAINER_NAME], stderr=subprocess.DEVNULL)

        self.tmpdir = tempfile.TemporaryDirectory()
        self.configs_dir = os.path.join(self.tmpdir.name, 'configs')
        self.data_dir = os.path.join(self.tmpdir.name, 'data')
        self.logs_dir = os.path.join(self.tmpdir.name, 'logs')
        for dirpath in [self.data_dir, self.configs_dir, self.logs_dir]:
            os.mkdir(dirpath)

        # Generate randomized backup filename because SSH server where backups are stored via SCP
        # is not cleared after each test.
        backup_filename_rand_tag = hex(random.getrandbits(32))[2:]
        self.backup_filename = f"supervisor-backup_{backup_filename_rand_tag}.bin"

        self.node = SSHConnInfo(
            host='localhost',
            user='somebody',
            port=2222,
            identity_file='test/config/ssh_id.key',
        )
        self.node_alias = SSHConnInfo(
            host='localhost',
            user='somebody',
            port=2223,
            identity_file='test/config/ssh_id.key',
        )

        key_desc, self.root_key = KeyDescriptor.generate(PASSWORD, 'argon2id_weak')
        self.auth_key = gen_user_key()
        self.exit_event = asyncio.Event()
        self.supervisor = ValidatorSupervisor(
            config=Config(
                eth2_network=ETH2_NETWORK,
                key_desc=key_desc,
                nodes=[self.node, self.node_alias],
                data_dir=self.data_dir,
                logs_dir=self.logs_dir,
                ssl_cert_file='test/config/cert.pem',
                ssl_key_file='test/config/key.pem',
                port_range=(13000, 14000),
                rpc_users={'admin': self.auth_key},
                backup_filename=self.backup_filename,
            ),
            root_key=self.root_key,
            exit_event=self.exit_event,
            enable_promtail=False,
            retry_delay=0,
            validator_container_name=CONTAINER_NAME,
        )

        self._generate_initial_backup()
        self.supervisor_task = asyncio.create_task(self.supervisor.run())

        client_ssl = ssl.SSLContext()
        client_ssl.load_verify_locations('test/config/cert.pem')
        client_ssl.verify_mode = ssl.CERT_REQUIRED
        self.remote_control = RpcClient(
            'admin',
            self.auth_key,
            self.supervisor.rpc_sock_path,
            client_ssl,
        )

    async def asyncTearDown(self):
        self.exit_event.set()
        await self.supervisor_task
        self.assertIsNone(self.supervisor._validator)

        self.tmpdir.cleanup()

    def _generate_initial_backup(self) -> None:
        make_validator_data_backup(
            self.root_key.derive_backup_key(),
            os.path.join(self.data_dir, self.backup_filename),
            'test/validator_data',
        )

    async def test_lighthouse_is_running_on_start(self):
        await self._wait_for_validator_running(timeout=5)
        lighthouse_pid = await self._wait_for_validator_process_running(timeout=5)

        result_proc = subprocess.run(
            ["ps", "--pid", str(lighthouse_pid), "-o", "command"],
            check=True,
            capture_output=True,
        )
        output = result_proc.stdout.decode()
        output_lines = output.splitlines()
        self.assertEqual(2, len(output_lines))
        self.assertRegex(output_lines[1].strip(), r"docker run .*lighthouse")

    async def test_remotely_stop_validator(self):
        await self._wait_for_validator_running(timeout=5)
        lighthouse_pid = await self._wait_for_validator_process_running(timeout=5)

        resp = await asyncio.wait_for(self.remote_control.stop_validator(), timeout=15)
        self.assertTrue(resp)
        result_proc = subprocess.run(["ps", "--pid", str(lighthouse_pid), "-o", "command"])
        self.assertEqual(1, result_proc.returncode)

        # Stopping when already stopped returns false
        resp = await asyncio.wait_for(self.remote_control.stop_validator(), timeout=15)
        self.assertFalse(resp)

    async def test_remote_connection_failure_when_supervisor_down(self):
        self.exit_event.set()
        await self.supervisor_task

        with self.assertRaises(ConnectionRefusedError):
            await self.remote_control.get_health()

    async def test_set_validator_release(self):
        await self._wait_for_validator_running(timeout=5)

        resp = await asyncio.wait_for(self.remote_control.stop_validator(), timeout=15)
        self.assertTrue(resp)

        old_release = ValidatorRelease(
            impl_name='lighthouse',
            version='v1.5.1',
            checksum='a44ecaf9a5f956e9e43928252d6471a2eb6dc59245a5747e4fb545d512522768',
        )
        await self.remote_control.set_validator_release(old_release)
        health_resp = await self.remote_control.get_health()
        self.assertEqual(health_resp['validator_release']['version'], old_release.version)

    async def test_connect_node(self):
        await self._wait_for_validator_running(timeout=5)

        def check_connected_to(host, port):
            async def check():
                health = await self.remote_control.get_health()
                return health['connected_node'] == [host, port]
            return check

        await self._wait_for(check_connected_to('localhost', 2222), 5)
        await self.remote_control.connect_eth2_node('localhost', 2223)
        await self._wait_for(check_connected_to('localhost', 2223), 5)

    async def _wait_for(self, check: Callable[[], Awaitable[bool]], timeout: float):
        poll_interval = 0.1
        for i in range(int(timeout / poll_interval)):
            await asyncio.sleep(poll_interval)
            if await check():
                return

        self.fail("timed out")

    async def _wait_for_validator_running(self, timeout: float):
        async def check() -> bool:
            resp = await asyncio.wait_for(self.remote_control.get_health(), timeout=2)
            self.assertIsInstance(resp, dict)
            self.assertTrue(resp['unlocked'])
            return bool(resp['validator_running'])
        await self._wait_for(check, timeout)

    async def _wait_for_validator_process_running(self, timeout: float) -> int:
        async def check() -> bool:
            if self.supervisor._validator is None:
                return False
            lighthouse_pid = self.supervisor._validator.get_pid()
            return lighthouse_pid is not None

        await self._wait_for(check, timeout)
        assert self.supervisor._validator is not None
        pid = self.supervisor._validator.get_pid()
        assert pid is not None
        return pid


if __name__ == '__main__':
    unittest.main()
