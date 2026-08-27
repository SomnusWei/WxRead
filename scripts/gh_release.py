import json,os,sys,ssl,http.client,tempfile
REPO="SomnusWei/WxRead"
TAG="v2.2.3"
TITLE="WxReadAssistant v2.2.3"
NOTES="## v2.2.3 Bug Fix\n\n- fix: today_seconds reset on new day\n- install: unzip and run exe\n"
PROXY_HOST="127.0.0.1";PROXY_PORT=7897
API_HOST="api.github.com";UPLOADS_HOST="uploads.github.com"
TOKEN=open(os.path.join(tempfile.gettempdir(),"gh_token.txt"),"r",encoding="utf-8-sig").read().strip()
os.remove(os.path.join(tempfile.gettempdir(),"gh_token.txt"))
ctx=ssl.create_default_context()
def api_call(host,method,path,headers,body=None):
    conn=http.client.HTTPSConnection(PROXY_HOST,PROXY_PORT,context=ctx)
    conn.set_tunnel(host,443)
    if body is not None and isinstance(body,(dict,list)):
        body=json.dumps(body).encode()
        headers["Content-Type"]="application/json"
    conn.request(method,path,body=body,headers=headers)
    resp=conn.getresponse()
    return resp.status,resp.read(),dict(resp.getheaders())
auth={"Authorization":f"token {TOKEN}","User-Agent":"WxRead-build","Accept":"application/vnd.github+json"}
print("1. Creating Release...")
status,data,_=api_call(API_HOST,"POST",f"/repos/{REPO}/releases",auth,{"tag_name":TAG,"name":TITLE,"body":NOTES,"draft":False,"prerelease":False})
print(f"   HTTP {status}")
if status==422:
    status,data,_=api_call(API_HOST,"GET",f"/repos/{REPO}/releases/tags/{TAG}",auth)
    print(f"   Exists: HTTP {status}")
if status not in (200,201):
    print(f"   ERROR: {data.decode(errors='replace')[:400]}")
    sys.exit(1)
release=json.loads(data)
rid=release["id"]
html_url=release.get("html_url","")
print(f"   ID={rid}")
zp="release/WxReadAssistant-v2.2.3-full.zip"
print(f"2. Uploading full.zip ({os.path.getsize(zp)//1048576}MB)...")
zd=open(zp,"rb").read()
h2=dict(auth)
h2["Content-Type"]="application/zip"
status,data,_=api_call(UPLOADS_HOST,"POST",f"/repos/{REPO}/releases/{rid}/assets?name=WxReadAssistant-v2.2.3-full.zip",h2,zd)
print(f"   HTTP {status} "+("OK" if status in (200,201) else data.decode(errors='replace')[:200]))
sp="release/WxReadAssistant-v2.2.3-full.sha1.txt"
print("3. Uploading sha1.txt...")
sd=open(sp,"rb").read()
h3=dict(auth)
h3["Content-Type"]="text/plain"
status2,data2,_=api_call(UPLOADS_HOST,"POST",f"/repos/{REPO}/releases/{rid}/assets?name=WxReadAssistant-v2.2.3-full.sha1.txt",h3,sd)
print(f"   HTTP {status2} "+("OK" if status2 in (200,201) else data2.decode(errors='replace')[:200]))
print(f"\nRelease: {html_url}")
