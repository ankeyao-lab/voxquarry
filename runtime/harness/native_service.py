"""Start official native providers, or bridge stdin to their local Unix socket."""
import json
import os
import signal
import socket
import sys
import threading


def main():
    if '--client' in sys.argv:
        value = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
        if len(value) > 4 * 1024 * 1024:
            raise ValueError('native request too large')
        json.loads(value)
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(30)
            connection.connect('/tmp/voxquarry-native.sock')
            connection.sendall(value.rstrip() + b'\n')
            size = 0
            while chunk := connection.recv(65536):
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    raise ValueError('native result too large')
                sys.stdout.buffer.write(chunk)
        return
    from deepseek_harness import DeepSeekHarness
    os.umask(0o077)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    # No model request is issued. The placeholder cannot authenticate anywhere;
    # this container has no network and receives no service credentials.
    with DeepSeekHarness(profile='sdk', cwd='/workspace', dsh_home='/tmp/native-home',
                         patches=('/opt/voxquarry/native-service.patch.yml',),
                         api_key='isolated-native-provider-no-model-access',
                         base_url='http://127.0.0.1:1', initialize_timeout_seconds=30):
        stop.wait()


if __name__ == '__main__':
    main()
