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
                message = '记忆检索未成功，授权可能已过期'
                print(json.dumps({'error': message}, ensure_ascii=False))
                return 1
            print(json.dumps(reply['result'], ensure_ascii=False))
            return 0
    except (OSError, ValueError, KeyError):
        print(json.dumps({'error': '记忆检索服务不可用或本轮授权已失效'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
