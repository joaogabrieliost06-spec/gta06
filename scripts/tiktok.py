#!/usr/bin/env python3
"""Envia os cortes prontos para o TikTok pela API oficial (Content Posting API).

Uso (dentro do workflow tiktok.yml):
  python scripts/tiktok.py conectar "<endereço que apareceu depois de Autorizar>"
  python scripts/tiktok.py postar            # posta o próximo corte da fila, se puder
  python scripts/tiktok.py fila              # só mostra a fila

Nada de senha: o robô guarda só o token OAuth que o TikTok entrega depois do
"Autorizar". O token fica criptografado em tiktok/token.enc (chave = Client secret,
que é um segredo do GitHub), porque o TikTok troca o refresh_token a cada renovação
e o workflow precisa salvar o novo.

Modos (variável TIKTOK_MODO do repositório):
  rascunho (padrão): o vídeo vai para a caixa de entrada do app do TikTok; você
                     abre, cola a legenda (está em tiktok/legendas.md) e publica.
                     Funciona com o app ainda sem auditoria (escopo video.upload).
  direto:            publica direto com a legenda (escopo video.publish). Sem a
                     auditoria do TikTok, só sai como privado (SELF_ONLY).

Limites anti-spam (variáveis opcionais):
  TIKTOK_POR_DIA      máximo de envios em 24 h (padrão 4; o TikTok aceita no máximo
                      5 rascunhos pendentes por 24 h)
  TIKTOK_INTERVALO_H  horas mínimas entre dois envios (padrão 3)
"""
import json, os, re, subprocess, sys, time, glob, urllib.parse, urllib.request, urllib.error

API = "https://open.tiktokapis.com"
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASTA = os.path.join(RAIZ, "tiktok")
TOKEN = os.path.join(PASTA, "token.enc")
POSTADOS = os.path.join(PASTA, "postados.json")
LEGENDAS = os.path.join(PASTA, "legendas.md")
REDIRECT = os.environ.get("TIKTOK_REDIRECT_URI", "https://github.com/joaogabrieliost06-spec/gta06")
CHUNK = 10_000_000


def env(nome, padrao=None):
    v = os.environ.get(nome, "").strip()
    return v if v else padrao


# ---------- HTTP ----------
def req(metodo, url, dados=None, headers=None, form=False, cru=None):
    h = dict(headers or {})
    corpo = None
    if cru is not None:
        corpo = cru
    elif dados is not None:
        if form:
            corpo = urllib.parse.urlencode(dados).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            corpo = json.dumps(dados).encode()
            h["Content-Type"] = "application/json; charset=UTF-8"
    r = urllib.request.Request(url, data=corpo, headers=h, method=metodo)
    try:
        with urllib.request.urlopen(r, timeout=300) as resp:
            txt = resp.read().decode() or "{}"
            return resp.status, txt
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def api(caminho, dados, token):
    st, txt = req("POST", API + caminho, dados, {"Authorization": f"Bearer {token}"})
    try:
        j = json.loads(txt)
    except ValueError:
        sys.exit(f"resposta estranha do TikTok ({st}): {txt[:500]}")
    erro = (j.get("error") or {})
    if erro.get("code", "ok") != "ok":
        sys.exit(f"TikTok recusou {caminho}: {erro.get('code')} - {erro.get('message')} (log_id {erro.get('log_id')})")
    return j.get("data") or {}


# ---------- token criptografado ----------
def _senha():
    s = env("TIKTOK_CLIENT_SECRET")
    if not s:
        sys.exit("falta o segredo TIKTOK_CLIENT_SECRET no GitHub")
    return s


def salvar_token(t):
    os.makedirs(PASTA, exist_ok=True)
    p = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-a",
                        "-pass", "env:TT_SENHA"], input=json.dumps(t).encode(),
                       capture_output=True, env={**os.environ, "TT_SENHA": _senha()}, check=True)
    open(TOKEN, "wb").write(p.stdout)


def ler_token():
    if not os.path.exists(TOKEN):
        sys.exit("TikTok ainda não conectado: rode o workflow '7 - TikTok' com acao=conectar")
    p = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-a",
                        "-pass", "env:TT_SENHA"], input=open(TOKEN, "rb").read(),
                       capture_output=True, env={**os.environ, "TT_SENHA": _senha()})
    if p.returncode:
        sys.exit("não consegui abrir tiktok/token.enc (o Client secret mudou?). Conecte de novo.")
    return json.loads(p.stdout)


def _guardar(j):
    agora = time.time()
    t = {"access_token": j["access_token"], "expira": agora + int(j.get("expires_in", 7200)) - 300,
         "refresh_token": j["refresh_token"],
         "refresh_expira": agora + int(j.get("refresh_expires_in", 15552000)),
         "open_id": j.get("open_id"), "scope": j.get("scope")}
    salvar_token(t)
    commit("tiktok: token renovado")  # o refresh antigo morre na hora; salvar já
    return t


def token_valido():
    t = ler_token()
    if time.time() < t["expira"]:
        return t
    st, txt = req("POST", API + "/v2/oauth/token/", {
        "client_key": env("TIKTOK_CLIENT_KEY"), "client_secret": _senha(),
        "grant_type": "refresh_token", "refresh_token": t["refresh_token"]}, form=True)
    j = json.loads(txt or "{}")
    if "access_token" not in j:
        sys.exit(f"não deu pra renovar o token ({st}): {txt[:300]}. Conecte de novo (acao=conectar).")
    return _guardar(j)


def conectar(endereco):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(endereco.strip()).query)
    code = (q.get("code") or [endereco.strip()])[0]
    if q.get("error"):
        sys.exit(f"o TikTok devolveu erro: {q['error'][0]} {q.get('error_description', [''])[0]}")
    st, txt = req("POST", API + "/v2/oauth/token/", {
        "client_key": env("TIKTOK_CLIENT_KEY"), "client_secret": _senha(), "code": code,
        "grant_type": "authorization_code", "redirect_uri": REDIRECT}, form=True)
    j = json.loads(txt or "{}")
    if "access_token" not in j:
        sys.exit(f"não deu pra conectar ({st}): {txt[:300]}. O código vale poucos minutos: autorize de novo e cole logo.")
    t = _guardar(j)
    print(f"TikTok conectado. Permissões: {t['scope']}")


# ---------- git ----------
def commit(msg):
    subprocess.run(["git", "add", "tiktok"], cwd=RAIZ, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=RAIZ).returncode == 0:
        return
    subprocess.run(["git", "commit", "-qm", msg], cwd=RAIZ, check=True)
    for i in range(8):
        if subprocess.run("git pull --rebase -q && git push -q", shell=True, cwd=RAIZ).returncode == 0:
            return
        time.sleep(4 * (i + 1))
    sys.exit("não consegui salvar no repositório (git push)")


# ---------- fila ----------
def ler_publicacao(job):
    p = os.path.join(RAIZ, "jobs", job, "publicacao.md")
    if not os.path.exists(p):
        return {}
    out, atual = {}, None
    for linha in open(p, encoding="utf-8"):
        m = re.match(r"##\s*(\d+)\s*[:\-–]\s*(.*)", linha)
        if m:
            atual = out.setdefault(m.group(1).zfill(2), {"nome": m.group(2).strip()})
            continue
        m = re.match(r"-\s*\*\*(T[ií]tulo|Descri[cç][aã]o|Hashtags):\*\*\s*(.*)", linha.strip())
        if m and atual is not None:
            chave = {"t": "titulo", "d": "descricao", "h": "hashtags"}[m.group(1)[0].lower()]
            atual[chave] = m.group(2).strip()
    return out


def montar_legenda(job, cid, info, canal):
    titulo = info.get("titulo") or info.get("nome") or ""
    titulo = titulo.replace('"', "").replace("“", "").replace("”", "").strip()
    desc = info.get("descricao", "")
    if "crédito" not in desc.lower() and "credito" not in desc.lower():
        desc = (desc + f" Créditos: {canal} – https://www.twitch.tv/{canal}").strip()
    tags = [t for t in info.get("hashtags", "").split() if t.lower() != "#shorts"]
    for extra in ("#fyp", "#cortes"):
        if extra not in tags:
            tags.append(extra)
    return f"{titulo}\n\n{desc}\n\n{' '.join(tags)}".strip()[:2200]


def fila(postados):
    itens = []
    for d in sorted(glob.glob(os.path.join(RAIZ, "jobs", "*")), reverse=True):  # live mais nova primeiro
        job = os.path.basename(d)
        if not os.path.exists(os.path.join(d, "entregue")) or not os.path.exists(os.path.join(d, "cortes.json")):
            continue
        try:
            cortes = json.load(open(os.path.join(d, "cortes.json"))).get("cortes") or []
        except ValueError:
            continue
        canal = "alanzoka"
        try:
            canal = json.load(open(os.path.join(d, "meta.json"))).get("canal") or canal
        except Exception:
            pass
        pub = ler_publicacao(job)
        for c in cortes:
            cid = str(c["id"]).zfill(2)
            if f"{job}/{cid}" in postados:
                continue
            info = pub.get(cid) or {"titulo": c.get("titulo", "")}
            itens.append({"job": job, "id": cid, "canal": canal,
                          "legenda": montar_legenda(job, cid, info, canal)})
    # Dentro do mesmo dia, em ordem da live (bloco mais cedo primeiro, corte 01, 02...)
    itens.sort(key=lambda x: (x["job"], x["id"]))
    itens.sort(key=lambda x: x["job"][:10], reverse=True)
    return itens


# ---------- envio ----------
def baixar(job, cid, destino):
    repo = env("GITHUB_REPOSITORY", "joaogabrieliost06-spec/gta06")
    nome = f"corte_{cid}_vertical.mp4"
    ids = subprocess.run(["gh", "api", f"repos/{repo}/releases/tags/{job}", "--jq",
                          f'.assets[] | select(.name=="{nome}") | .id'],
                         capture_output=True, text=True).stdout.split()
    if not ids:
        return False
    with open(destino, "wb") as f:
        subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                        f"repos/{repo}/releases/assets/{ids[0]}"], stdout=f, check=True)
    return os.path.getsize(destino) > 0


def enviar(t, caminho, legenda, modo):
    tam = os.path.getsize(caminho)
    if tam < 5_000_000:
        chunk, n = tam, 1
    else:
        chunk = CHUNK
        n = max(1, tam // chunk)
    fonte = {"source": "FILE_UPLOAD", "video_size": tam, "chunk_size": chunk, "total_chunk_count": n}
    if modo == "direto":
        info = api("/v2/post/publish/creator_info/query/", {}, t["access_token"])
        opcoes = info.get("privacy_level_options") or ["SELF_ONLY"]
        priv = env("TIKTOK_PRIVACIDADE", "PUBLIC_TO_EVERYONE")
        if priv not in opcoes:
            priv = "SELF_ONLY" if "SELF_ONLY" in opcoes else opcoes[0]
        print(f"publicando direto, privacidade {priv}")
        d = api("/v2/post/publish/video/init/", {
            "post_info": {"title": legenda, "privacy_level": priv, "disable_duet": False,
                          "disable_stitch": False, "disable_comment": False,
                          "brand_content_toggle": False, "brand_organic_toggle": False},
            "source_info": fonte}, t["access_token"])
    else:
        d = api("/v2/post/publish/inbox/video/init/", {"source_info": fonte}, t["access_token"])
    url, pid = d["upload_url"], d["publish_id"]
    with open(caminho, "rb") as f:
        for i in range(n):
            ini = i * chunk
            fim = tam - 1 if i == n - 1 else ini + chunk - 1
            f.seek(ini)
            pedaco = f.read(fim - ini + 1)
            for tent in range(4):
                st, txt = req("PUT", url, cru=pedaco, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(pedaco)),
                    "Content-Range": f"bytes {ini}-{fim}/{tam}"})
                if st in (200, 201, 206):
                    break
                print(f"pedaço {i+1}/{n} falhou ({st}) {txt[:200]}; tentando de novo")
                time.sleep(5 * (tent + 1))
            else:
                sys.exit("envio do vídeo falhou")
            print(f"pedaço {i+1}/{n} enviado")
    status = "?"
    for _ in range(40):
        time.sleep(10)
        s = api("/v2/post/publish/status/fetch/", {"publish_id": pid}, t["access_token"])
        status = s.get("status", "?")
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            break
        if status == "FAILED":
            sys.exit(f"TikTok não aceitou o vídeo: {s.get('fail_reason')}")
    return pid, status


def postar():
    postados = json.load(open(POSTADOS)) if os.path.exists(POSTADOS) else {}
    agora = time.time()
    por_dia = int(env("TIKTOK_POR_DIA", "4"))
    intervalo = float(env("TIKTOK_INTERVALO_H", "3")) * 3600
    recentes = [v["quando"] for v in postados.values() if agora - v["quando"] < 86400]
    if len(recentes) >= por_dia:
        print(f"já foram {len(recentes)} envios nas últimas 24 h (limite {por_dia}); fica pra depois")
        return
    if recentes and agora - max(recentes) < intervalo:
        print(f"último envio faz {(agora - max(recentes))/3600:.1f} h; espero {intervalo/3600:.0f} h entre envios")
        return
    itens = fila(postados)
    if not itens:
        print("fila vazia: nenhum corte novo pra enviar")
        return
    modo = env("TIKTOK_MODO", "rascunho")
    t = token_valido()
    for it in itens[:3]:  # se um corte não tiver vídeo na Release, tenta o próximo
        video = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "tiktok.mp4")
        if not baixar(it["job"], it["id"], video):
            print(f"{it['job']} corte {it['id']}: vídeo não está na Release, pulando")
            continue
        print(f"enviando {it['job']} corte {it['id']} ({os.path.getsize(video)/1e6:.0f} MB), modo {modo}")
        pid, status = enviar(t, video, it["legenda"], modo)
        n = len(postados) + 1
        postados[f"{it['job']}/{it['id']}"] = {"quando": agora, "publish_id": pid, "modo": modo,
                                               "status": status, "numero": n}
        os.makedirs(PASTA, exist_ok=True)
        json.dump(postados, open(POSTADOS, "w"), indent=1, ensure_ascii=False)
        if modo != "direto":
            antigo = open(LEGENDAS, encoding="utf-8").read() if os.path.exists(LEGENDAS) else ""
            topo = "# Legendas pra colar no TikTok\n\nO mais novo fica em cima. Abra o app do TikTok, toque no aviso do rascunho, cole a legenda e publique.\n\n"
            antigo = antigo.replace(topo, "")
            bloco = (f"## #{n}: {it['job']} corte {it['id']} ({time.strftime('%d/%m %H:%M UTC', time.gmtime(agora))})\n\n"
                     f"```\n{it['legenda']}\n```\n\n")
            open(LEGENDAS, "w", encoding="utf-8").write(topo + bloco + antigo)
        commit(f"tiktok: enviado {it['job']} corte {it['id']} ({status})")
        print(f"pronto: {status}\n--- legenda ---\n{it['legenda']}")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(
                f"### Enviado pro TikTok: {it['job']} corte {it['id']} ({status})\n\n```\n{it['legenda']}\n```\n")
        return
    print("nenhum dos próximos cortes tinha vídeo na Release")


if __name__ == "__main__":
    acao = sys.argv[1] if len(sys.argv) > 1 else "postar"
    if acao == "conectar":
        conectar(sys.argv[2] if len(sys.argv) > 2 else env("TIKTOK_ENDERECO", ""))
    elif acao == "fila":
        for it in fila(json.load(open(POSTADOS)) if os.path.exists(POSTADOS) else {}):
            print(f"{it['job']} {it['id']}: {it['legenda'].splitlines()[0]}")
    else:
        postar()
