"""Owner FULL-agent entry to the same bridge-authorized memory_search tool.

Only grant path and query are accepted. No roots, scopes or identities come
from the model. This is not a standalone filesystem search command.
"""
import argparse
import json
import socket
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--grant', required=True)
    parser.add_argument('--query', required=True)
    args = parser.parse_args()
    try:
        env = json.loads(Path(args.grant).read_text())['mcpServers']['qq']['env']
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(30)
            conn.connect(env['QQ_TOOL_SOCKET'])
            req = {'token': env['QQ_TOOL_TOKEN'], 'name': 'memory_search', 'arguments': {'query': args.query}}
            conn.sendall((json.dumps(req, ensure_ascii=False) + '\n').encode())
            with conn.makefile('rb') as stream:
                raw = stream.readline(65537)
            if not raw or len(raw) > 65536:
                raise ValueError()
            reply = json.loads(raw)
            if not reply.get('ok'):
                reason = reply.get('error')
                # Diary errors are fixed service messages, never file contents
                # or arbitrary OS exceptions. Do not mislabel a busy organizer
                # as an expired capability.
                diary_errors = {
                    '日记正在整理，请稍后再查', '日记同步失败，本次未使用旧副本回答',
                    '日记目录超过同步扫描上限', '日记数量超过本阶段同步上限',
                    '日记总大小超过同步上限', '日记超过单文件同步上限',
                    '日记必须是无链接的普通文件', '日记在读取中发生变化，请稍后重试',
                    '日记不是有效文本', '日记同步锁不可用',
                }
                message = reason if isinstance(reason, str) and reason in diary_errors else '记忆检索未成功，授权可能已过期'
                print(json.dumps({'error': message}, ensure_ascii=False))
                return 1
            print(json.dumps(reply['result'], ensure_ascii=False))
            return 0
    except (OSError, ValueError, KeyError):
        print(json.dumps({'error': '记忆检索服务不可用或本轮授权已失效'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
