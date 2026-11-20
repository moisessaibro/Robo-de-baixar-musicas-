"""
Para cada link do chat do WhatsApp (reels do Facebook):
  1. abre o link no navegador (visível),
  2. descobre o nome da música (rótulo de música do Facebook; se for
     "áudio original", tenta reconhecer o som com o Shazam),
  3. abre o YouTube, digita o nome da música na busca e clica no 1º resultado,
  4. baixa o áudio desse vídeo em MP3.

Instalação (uma vez só, no Prompt de Comando):
    pip install -U selenium yt-dlp shazamio
    winget install ffmpeg
(se o shazamio der erro ao instalar, pode ignorar: o script funciona sem ele,
só não reconhece músicas quando o reel usa "áudio original")

Uso:
    python baixar_musicas_navegador.py
    python baixar_musicas_navegador.py "Conversa do WhatsApp com musicas pendrive.zip"
"""

import asyncio
import json
import os
import random
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from shazamio import Shazam
except Exception:  # shazamio é opcional
    Shazam = None

# ----------------------------- configurações -----------------------------
PASTA_SAIDA = Path("musicas_mp3")
PASTA_PERFIL = Path("perfil_navegador")      # guarda seu login do Facebook
ARQ_PROCESSADOS = Path("ja_processados.txt")
ARQ_NAO_IDENTIFICADAS = Path("nao_identificadas.txt")
ARQ_FALHAS = Path("falhas.txt")
ARQ_LOG = Path("registro_musicas.txt")       # link | música | vídeo do YouTube
URL_Y2META = "https://y2meta.is/pt94/"       # aberto numa nova aba depois do YouTube
SEGUNDOS_Y2META = 5                          # quanto tempo deixar essa aba aberta
ARQ_COOKIES = Path("cookies_youtube.txt")    # gerado sozinho (não compartilhe: tem seu login)
SUFIXO_BUSCA = " official audio"             # ajuda a achar a versão de estúdio
PERFIL_CHROME = "crossfireevargas"           # nome (ou e-mail) do perfil do Chrome a usar
# -------------------------------------------------------------------------

REGEX_LINK = re.compile(r"https?://[^\s<>\"']+")
DOMINIOS = ("youtube.com", "youtu.be", "soundcloud.com", "facebook.com", "fb.watch")


# ------------------------------ chat/links -------------------------------
def ler_chat(caminho: Path) -> str:
    if caminho.suffix.lower() == ".zip":
        with zipfile.ZipFile(caminho) as z:
            nome_txt = next(n for n in z.namelist() if n.lower().endswith(".txt"))
            return z.read(nome_txt).decode("utf-8", errors="ignore")
    return caminho.read_text(encoding="utf-8", errors="ignore")


def extrair_links(texto: str) -> list:
    vistos, links = set(), []
    for bruto in REGEX_LINK.findall(texto):
        link = bruto.rstrip(".,;:!?)]}")
        if any(d in link for d in DOMINIOS) and link not in vistos:
            vistos.add(link)
            links.append(link)
    return links


def achar_arquivo_chat() -> Path:
    ignorar = {ARQ_PROCESSADOS.name, ARQ_NAO_IDENTIFICADAS.name, ARQ_FALHAS.name,
               ARQ_LOG.name, ARQ_COOKIES.name}
    candidatos = [
        p for p in list(Path(".").glob("*.txt")) + list(Path(".").glob("*.zip"))
        if p.name not in ignorar
    ]
    if not candidatos:
        sys.exit("Nenhum .txt/.zip de chat encontrado nesta pasta.")
    return max(candidatos, key=lambda p: p.stat().st_mtime)


def registrar(arquivo: Path, linha: str):
    with arquivo.open("a", encoding="utf-8") as f:
        f.write(linha + "\n")


# ------------------------------- navegador -------------------------------
def achar_perfil_chrome(nome: str):
    """Procura, no Chrome instalado, a pasta do perfil cujo nome/e-mail contém `nome`."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    estado = base / "Local State"
    if not estado.exists():
        return None, None
    try:
        info = json.loads(estado.read_text(encoding="utf-8")).get("profile", {}).get("info_cache", {})
    except Exception:
        return base, None
    alvo = nome.lower()
    for pasta, dados in info.items():
        campos = [str(dados.get(k, "")).lower() for k in ("name", "gaia_name", "user_name")]
        if any(alvo in c for c in campos):
            return base, pasta
    print(f"Perfil '{nome}' não encontrado. Perfis do seu Chrome:")
    for pasta, dados in info.items():
        print(f"   - {dados.get('name')} ({dados.get('user_name', '')}) -> pasta {pasta}")
    return base, None


def preparar_copia_perfil(base: Path, pasta: str) -> Path:
    """
    O Chrome novo (v136+) não deixa o Selenium controlar a pasta de perfil
    padrão. Por isso o script usa uma CÓPIA do seu perfil (feita uma vez só).
    """
    destino = PASTA_PERFIL / "chrome_copia"
    if not (destino / pasta).exists():
        print(f"Copiando o perfil '{pasta}' do Chrome (só na primeira vez; feche o Chrome antes)...")
        destino.mkdir(parents=True, exist_ok=True)
        shutil.copy2(base / "Local State", destino / "Local State")
        ignorar = shutil.ignore_patterns(
            "Cache", "Code Cache", "GPUCache", "Service Worker", "GrShaderCache",
            "ShaderCache", "DawnCache", "Crashpad", "Singleton*", "*.lock",
        )
        try:
            shutil.copytree(base / pasta, destino / pasta, ignore=ignorar, dirs_exist_ok=True)
        except shutil.Error:
            pass  # alguns arquivos em uso não copiam; o resto basta
    return destino


def abrir_navegador():
    erros = []
    for nome, Opcoes, Driver in (
        ("chrome", webdriver.ChromeOptions, webdriver.Chrome),
        ("edge", webdriver.EdgeOptions, webdriver.Edge),
    ):
        try:
            op = Opcoes()
            dados = (PASTA_PERFIL / nome).resolve()
            if nome == "chrome" and PERFIL_CHROME:
                base, pasta = achar_perfil_chrome(PERFIL_CHROME)
                if pasta:
                    dados = preparar_copia_perfil(base, pasta).resolve()
                    op.add_argument(f"--profile-directory={pasta}")
                    print(f"Usando o perfil do Chrome '{PERFIL_CHROME}' (pasta {pasta})")
                else:
                    print("Usando um perfil novo do Chrome (login será pedido uma vez).")
            op.add_argument(f"--user-data-dir={dados}")
            op.add_argument("--start-maximized")
            op.add_argument("--mute-audio")
            op.add_argument("--disable-notifications")
            op.add_experimental_option("excludeSwitches", ["enable-automation"])
            driver = Driver(options=op)
            print(f"Navegador aberto: {nome}")
            return driver
        except WebDriverException as e:
            erros.append(f"{nome}: {str(e)[:150]}")
    sys.exit("Não consegui abrir Chrome nem Edge:\n" + "\n".join(erros))


def limpar_nome(texto: str) -> str:
    texto = texto.replace("·", " ").replace("•", " ")
    texto = "".join(c for c in texto if ord(c) <= 0xFFFF)  # o chromedriver não digita emojis
    return re.sub(r"\s+", " ", texto).strip()


JS_ROTULOS = r"""
const achados = [];
const vistos = new Set();
function add(t) {
  t = (t || '').replace(/\s+/g, ' ').trim();
  if (t && t.length < 150 && !vistos.has(t)) { vistos.add(t); achados.push(t); }
}
// 1) links de áudio/música do Facebook
document.querySelectorAll('a[href*="/reels/audio/"], a[href*="/audio/"], a[href*="/music/"]')
  .forEach(a => add(a.innerText || a.getAttribute('aria-label')));
// 2) rótulo no formato "Artista · Música" (ignora "Seguir", datas etc.)
document.querySelectorAll('span, div').forEach(el => {
  if (el.children.length > 2) return;
  const t = (el.innerText || '').trim();
  if (t.length > 3 && t.length < 100 && t.includes(' · ') &&
      !/seguir|seguindo|follow|\d+\s?(h|min|sem|d|m|s)\b/i.test(t)) add(t);
});
return achados;
"""


def textos_de_audio(driver) -> list:
    """Textos dos rótulos de música que o Facebook mostra no reel."""
    try:
        return [t for t in driver.execute_script(JS_ROTULOS) if t]
    except WebDriverException:
        return []


def nome_no_codigo_fonte(driver):
    """Plano B: procura título/artista da música dentro dos dados da página."""
    try:
        html = driver.page_source
    except WebDriverException:
        return None

    def achar(chaves):
        m = re.search(r'"(?:%s)"\s*:\s*"([^"]{2,100})"' % chaves, html)
        if not m:
            return ""
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return m.group(1)

    titulo = achar("music_title|song_name|track_title|audio_title|music_attribution_title")
    artista = achar("artist_name|music_artist|artist_display_name")
    nome = limpar_nome(f"{artista} {titulo}")
    if titulo and "original" not in nome.lower():
        return nome
    return None


def salvar_debug(driver, link: str):
    """Quando não acha nada, guarda print + texto da página para investigarmos."""
    pasta = Path("debug_facebook")
    pasta.mkdir(exist_ok=True)
    base = pasta / re.sub(r"[^A-Za-z0-9]+", "_", link)[-60:]
    try:
        driver.save_screenshot(str(base) + ".png")
        (Path(str(base) + ".txt")).write_text(
            driver.find_element(By.TAG_NAME, "body").text, encoding="utf-8")
        print(f"   (debug salvo em {pasta}/)")
    except Exception:
        pass


def nome_do_rotulo(texto: str) -> str:
    """O Facebook repete o rótulo ('Artista · Música Artista · Música ...'); fica só uma vez."""
    partes = [p.strip() for p in texto.split("·")]
    if len(partes) < 2:
        return texto
    artista, musica = partes[0], partes[1]
    if artista and musica.endswith(artista):
        musica = musica[: -len(artista)].strip()
    return f"{artista} · {musica}"


def nome_musica_facebook(driver, link: str):
    driver.get(link)
    textos = []
    limite = time.time() + 20
    while time.time() < limite and not textos:   # espera o rótulo aparecer
        time.sleep(1)
        textos = textos_de_audio(driver)

    if textos:
        print(f"   Rótulos encontrados: {textos[:4]}")
        for texto in textos:
            baixo = texto.lower()
            if "áudio original" in baixo or "original audio" in baixo:
                return None  # sem música identificada pelo Facebook
            return limpar_nome(nome_do_rotulo(texto))

    nome = nome_no_codigo_fonte(driver)
    if nome:
        return nome
    salvar_debug(driver, link)
    return None


# -------------------------------- Shazam ---------------------------------
async def _reconhecer(caminho: str):
    shazam = Shazam()
    if hasattr(shazam, "recognize"):
        return await shazam.recognize(caminho)
    return await shazam.recognize_song(caminho)


def identificar_shazam(link: str):
    if Shazam is None:
        return None
    pasta = Path("temp_audio")
    shutil.rmtree(pasta, ignore_errors=True)
    pasta.mkdir()
    opcoes = {
        "format": "bestaudio/best",
        "outtmpl": str(pasta / "clip.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
    }
    try:
        with yt_dlp.YoutubeDL(opcoes) as ydl:
            ydl.download([link])
        arquivos = list(pasta.glob("clip.*"))
        if not arquivos:
            return None
        resultado = asyncio.run(_reconhecer(str(arquivos[0])))
    except Exception as e:
        print(f"   (Shazam falhou: {str(e)[:100]})")
        return None
    finally:
        shutil.rmtree(pasta, ignore_errors=True)
    faixa = (resultado or {}).get("track")
    if not faixa:
        return None
    return limpar_nome(f"{faixa.get('subtitle', '')} {faixa.get('title', '')}")


# -------------------------------- YouTube --------------------------------
def url_limpa(url: str) -> str:
    v = parse_qs(urlparse(url).query).get("v")
    return f"https://www.youtube.com/watch?v={v[0]}" if v else url


def pesquisar_e_abrir_youtube(driver, consulta: str) -> str:
    driver.get("https://www.youtube.com/")
    espera = WebDriverWait(driver, 20)
    caixa = espera.until(EC.element_to_be_clickable((By.NAME, "search_query")))
    caixa.click()
    caixa.clear()
    for letra in consulta:               # digita devagar para você acompanhar
        caixa.send_keys(letra)
        time.sleep(0.06)
    time.sleep(0.6)
    caixa.send_keys(Keys.ENTER)

    primeiro = espera.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "ytd-video-renderer a#video-title"))
    )
    time.sleep(1.5)
    primeiro.click()
    espera.until(lambda d: "/watch" in d.current_url)
    time.sleep(3)
    try:
        driver.execute_script("document.querySelector('video').pause()")
    except WebDriverException:
        pass
    return url_limpa(driver.current_url)


def abrir_y2meta(driver, url_yt: str):
    """Nova aba: abre o Google, entra no y2meta e cola o link do vídeo; depois fecha a aba."""
    aba_original = driver.current_window_handle
    driver.switch_to.new_window("tab")
    try:
        driver.get("https://www.google.com/")
        time.sleep(1.5)
        driver.get(URL_Y2META)
        time.sleep(3)
        try:  # tenta colar o link do YouTube no campo da página
            campo = WebDriverWait(driver, 10).until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, 'input[type="text"], input[type="url"], input:not([type])')))
            campo.click()
            campo.clear()
            campo.send_keys(url_yt)
        except Exception:
            print("   (y2meta: não achei o campo para colar o link)")
        time.sleep(SEGUNDOS_Y2META)
    except WebDriverException as e:
        print(f"   (y2meta falhou: {str(e)[:100]})")
    finally:
        try:
            driver.close()
        except WebDriverException:
            pass
        driver.switch_to.window(aba_original)


def exportar_cookies(driver):
    """Salva os cookies do navegador (já logado) para o yt-dlp não ser tratado como robô."""
    try:
        if "youtube.com" not in driver.current_url:
            driver.get("https://www.youtube.com/")
            time.sleep(2)
        cookies = driver.get_cookies()
    except WebDriverException:
        return
    futuro = int(time.time()) + 365 * 24 * 3600
    linhas = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        dominio = c.get("domain", "")
        linhas.append("\t".join([
            dominio,
            "TRUE" if dominio.startswith(".") else "FALSE",
            c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c.get("expiry", futuro))),
            c["name"],
            c["value"],
        ]))
    ARQ_COOKIES.write_text("\n".join(linhas) + "\n", encoding="utf-8")


def baixar_mp3(url: str):
    opcoes = {
        "format": "bestaudio/best",
        "outtmpl": str(PASTA_SAIDA / "%(title).80s [%(id)s].%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,
        "no_warnings": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    if ARQ_COOKIES.exists():
        opcoes["cookiefile"] = str(ARQ_COOKIES)
    with yt_dlp.YoutubeDL(opcoes) as ydl:
        ydl.download([url])


# --------------------------------- main ----------------------------------
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    arquivo = Path(args[0]) if args else achar_arquivo_chat()
    print(f"Lendo: {arquivo}")
    links = extrair_links(ler_chat(arquivo))
    feitos = set(ARQ_PROCESSADOS.read_text(encoding="utf-8").split()) if ARQ_PROCESSADOS.exists() else set()
    if "--retentar" in sys.argv and ARQ_NAO_IDENTIFICADAS.exists():
        feitos -= set(ARQ_NAO_IDENTIFICADAS.read_text(encoding="utf-8").split())
        ARQ_NAO_IDENTIFICADAS.unlink()
    pendentes = [l for l in links if l not in feitos]
    print(f"{len(links)} links encontrados, {len(pendentes)} ainda não processados.\n")
    if not pendentes:
        return

    PASTA_SAIDA.mkdir(exist_ok=True)
    driver = abrir_navegador()
    ok = nao_ident = falhas = 0
    try:
        driver.get("https://www.facebook.com/")
        input("Se o Facebook pedir, faça login na janela do navegador.\n"
              "Quando estiver pronto, volte aqui e aperte Enter... ")

        for i, link in enumerate(pendentes, 1):
            print(f"\n[{i}/{len(pendentes)}] {link}")
            try:
                if "facebook.com" in link or "fb.watch" in link:
                    nome = nome_musica_facebook(driver, link)
                    if nome:
                        print(f"   Música no Facebook: {nome}")
                    else:
                        print("   Sem nome no Facebook, tentando Shazam...")
                        nome = identificar_shazam(link)
                    if not nome:
                        print("   ✗ não consegui identificar a música")
                        registrar(ARQ_NAO_IDENTIFICADAS, link)
                        registrar(ARQ_PROCESSADOS, link)
                        nao_ident += 1
                        continue
                    print(f"   Pesquisando no YouTube: {nome}")
                    url_yt = pesquisar_e_abrir_youtube(driver, nome + SUFIXO_BUSCA)
                else:
                    nome, url_yt = "(link direto)", link

                print(f"   Abrindo o y2meta...")
                abrir_y2meta(driver, url_yt)
                print(f"   Baixando: {url_yt}")
                exportar_cookies(driver)
                baixar_mp3(url_yt)
                registrar(ARQ_LOG, f"{link} | {nome} | {url_yt}")
                registrar(ARQ_PROCESSADOS, link)
                ok += 1
            except Exception as e:
                print(f"   ✗ falhou: {str(e)[:150]}")
                registrar(ARQ_FALHAS, link)
                falhas += 1
            time.sleep(random.uniform(2, 4))
    finally:
        driver.quit()

    print(f"\nConcluído: {ok} baixadas, {nao_ident} não identificadas, {falhas} com erro.")
    print(f"MP3s em: {PASTA_SAIDA.resolve()}")
    print(f"Confira o que foi baixado de cada link em: {ARQ_LOG}")
    try:
        os.startfile(PASTA_SAIDA.resolve())   # abre a pasta no Explorador (Windows)
    except Exception:
        pass


if __name__ == "__main__":
    main()
