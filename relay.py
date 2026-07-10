from flask import Flask, request, Response
import requests

app = Flask(__name__)

@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def relay(path):
    url = f"https://www.okx.com/{path}"
    headers = {k: v for k, v in request.headers if k.lower() != "host"}
    resp = requests.request(
        method=request.method, url=url, headers=headers,
        params=request.args, data=request.get_data(), timeout=30
    )
    # Strip problematic headers
    excluded = {"transfer-encoding", "content-encoding", "content-length"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
    return Response(resp.content, status=resp.status_code, headers=resp_headers)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
