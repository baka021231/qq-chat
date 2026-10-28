"""Owner FULL-agent entry to the bridge-authorized, per-turn memory tool."""
import argparse
import json
import socket
from pathlib import Path


def failure(code, message):
    # Fixed messages only: never echo token/config contents or server traces.
    print(json.dumps({'error': message, 'code': code}, ensure_ascii=False))
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--grant', required=True)
    parser.add_argument('--query', required=True)
    args = parser.parse_args()
    try:
        env = json.loads(Path(args.grant).read_text())['mcpServers']['qq']['env']
        socket_path, token = env['QQ_TOOL_SOCKET'], env['QQ_TOOL_TOKEN']
        if not isinstance(socket_path, str) or not isinstance(token, str):
            raise ValueError()
    except FileNotFoundError:
        return failure('grant_missing', '本轮授权文件不存在，请使用当前轮提供的命令，不能复用历史路径')
    except (OSError, ValueError, KeyError, TypeError):
        return failure('grant_invalid', '本轮授权文件无法读取或格式无效')
    try:
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(30)
            conn.connect(socket_path)
            req = {'token': token, 'name': 'memory_search', 'arguments': {'query': args.query}}
            conn.sendall((json.dumps(req, ensure_ascii=False) + '\n').encode())
            with conn.makefile('rb') as stream:
                raw = stream.readline(65537)
    except TimeoutError:
        return failure('service_timeout', '记忆检索服务响应超时')
    except OSError:
        return failure('service_unavailable', '无法连接记忆检索服务')
    try:
        if not raw or len(raw) > 65536:
            raise ValueError()
        reply = json.loads(raw)
        if not isinstance(reply, dict):
            raise ValueError()
        if not reply.get('ok'):
            if reply.get('error') == '工具授权已失效':
                return failure('grant_expired', '本轮授权已到期或撤销，请使用当前轮提供的命令')
            return failure('request_rejected', '记忆检索请求被服务拒绝，请检查参数与工具状态')
        print(json.dumps(reply['result'], ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError):
        return failure('invalid_response', '记忆检索服务返回了无效响应')


if __name__ == '__main__':
    raise SystemExit(main())
