"""Standard-library MCP shim, executable with python -I and no package imports."""
import json
import os
import socket
import sys


def bridge_call(name, arguments):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(30)
        connection.connect(os.environ['QQ_TOOL_SOCKET'])
        request = {'token': os.environ['QQ_TOOL_TOKEN'], 'name': name, 'arguments': arguments}
        connection.sendall((json.dumps(request, ensure_ascii=False) + '\n').encode())
        with connection.makefile('rb') as stream:
            raw = stream.readline(65537)
        if not raw or len(raw) > 65536:
            raise ValueError('工具服务返回无效响应')
        reply = json.loads(raw)
        if not reply.get('ok'):
            raise ValueError(reply.get('error', '工具调用失败'))
        return reply['result']


def handle(message):
    method = message.get('method')
    if method == 'initialize':
        return {'protocolVersion': message.get('params', {}).get('protocolVersion', '2025-06-18'),
                'capabilities': {'tools': {}}, 'serverInfo': {'name': 'qq', 'version': '1'}}
    if method == 'tools/list':
        return {'tools': bridge_call('tools/list', {})}
    if method == 'tools/call':
        params = message.get('params', {})
        try:
            result = bridge_call(params.get('name'), params.get('arguments', {}))
            return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}
        except Exception as error:
            return {'isError': True, 'content': [{'type': 'text', 'text': str(error) if isinstance(error, ValueError) else '桥接工具服务不可用'}]}
    if method == 'ping':
        return {}
    raise ValueError('Unsupported MCP method')


def main():
    for line in sys.stdin:
        message = {}
        try:
            if len(line) > 65536: raise ValueError('MCP message too large')
            message = json.loads(line)
            if message.get('id') is None: continue
            response = {'jsonrpc': '2.0', 'id': message['id'], 'result': handle(message)}
        except Exception:
            response = {'jsonrpc': '2.0', 'id': message.get('id'), 'error': {'code': -32603, 'message': 'MCP request failed'}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
