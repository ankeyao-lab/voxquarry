"""Reuse the official Python Session notification/finish loop for restored agents."""
import json
import socket
from types import SimpleNamespace


def _request(value):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(30)
        connection.connect('/state/voxquarry-resume.sock')
        connection.sendall(json.dumps(value).encode() + b'\n')
        parts, size = [], 0
        while chunk := connection.recv(4096):
            parts.append(chunk)
            size += len(chunk)
            if size > 65536:
                raise RuntimeError('Invalid resume response')
    result = json.loads(b''.join(parts))
    if 'error' in result:
        raise RuntimeError(result['error'])
    return result


def session_exists(session_id):
    return _request({'operation': 'exists', 'sessionId': session_id})['exists'] is True


class ResumeClient:
    def __init__(self, client):
        self.client = client

    def subscribe_session_notifications(self, session_id):
        return self.client.subscribe_session_notifications(session_id)

    def session_prompt(self, session_id, content_blocks, **_options):
        value = _request({'sessionId': session_id, 'contentBlocks': content_blocks})
        return value['messageId']


def resume_session(harness, session_id, prompt, on_notification=None):
    from deepseek_harness.api import Session
    return Session(SimpleNamespace(client=ResumeClient(harness.client)), session_id).run(
        prompt, on_notification=on_notification)
