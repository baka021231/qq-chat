"""Explicit live test: sends matching text and synthesized speech to one target.

Unlike the offline unit tests, this command sends real QQ messages.
"""
import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_chat.bridge import BASE, Bridge, Source
from websockets.asyncio.client import connect


async def deliver(cfg, source, text):
    bridge = Bridge(cfg)
    if bridge.voice is None:
        bridge.db.close()
        raise ValueError('Enable voice in configuration before testing')
    try:
        async with connect(cfg['ws_url'], additional_headers={
            'Authorization': 'Bearer ' + cfg['onebot_token']}, proxy=None) as ws:
            bridge.ws = ws
            async def responses():
                async for payload in ws:
                    event = json.loads(payload)
                    future = bridge.pending.get(event.get('echo'))
                    if future is not None and not future.done():
                        future.set_result(event)
            reader = asyncio.create_task(responses())
            try:
                # Fail the live test if fallback occurs, rather than claiming success.
                failures = []
                async def notice(source, text): failures.append(text)
                bridge.voice.send_notice = notice
                await bridge.send(source, text)
                await bridge.enqueue_voice(source, text)
                await asyncio.wait_for(bridge.voice.queue.join(), 240)
                if failures:
                    raise RuntimeError('QQ speech delivery failed')
                print('Text and speech acknowledged by QQ')
            finally:
                await bridge.voice.close()
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
    finally:
        bridge.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--group', type=int)
    target.add_argument('--private-owner', action='store_true')
    parser.add_argument('--text', default='同桌，你好。我是爱弥斯，这是一条语音测试。')
    args = parser.parse_args()
    cfg = json.loads((BASE / 'config.json').read_text())
    owner = int(cfg['owner_qq'])
    source = Source('group', args.group, owner) if args.group else Source('private', owner, owner)
    asyncio.run(deliver(cfg, source, args.text))


if __name__ == '__main__':
    main()
