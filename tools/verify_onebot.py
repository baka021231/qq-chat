import asyncio,json,uuid
from pathlib import Path
from websockets.asyncio.client import connect
async def main():
 c=json.loads((Path(__file__).resolve().parents[1] / 'config.json').read_text())
 async with connect(c['ws_url'],additional_headers={'Authorization':'Bearer '+c['onebot_token']},proxy=None) as ws:
  for action in ('get_status','get_login_info'):
   echo=uuid.uuid4().hex
   await ws.send(json.dumps({'action':action,'echo':echo}))
   async with asyncio.timeout(15):
    while True:
     m=json.loads(await ws.recv())
     if m.get('echo')==echo:break
   assert m.get('status')=='ok' and m.get('retcode')==0,(action,'failed')
   if action=='get_status':assert m['data'].get('online') is True and m['data'].get('good') is True
   print(action,'OK')
asyncio.run(main())
