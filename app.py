#!/usr/bin/env python3
"""
TubeGrab — Backend Flask
Requiert : pip install flask flask-cors yt-dlp
Optionnel : ffmpeg installé sur le système (pour MP3 et fusion vidéo HD)
"""

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import re
import glob
import tempfile
import threading
import time

app = Flask(__name__)

@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "index.html"))

# Autoriser les requêtes depuis le fichier HTML local et localhost
CORS(app, origins=["*"])

# ── Helpers ──────────────────────────────────────────────────────────────────

def format_duration(seconds):
    """Convertit des secondes en mm:ss ou hh:mm:ss."""
    if not seconds:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_filename(name, max_len=80):
    """Nettoie un titre pour en faire un nom de fichier valide."""
    name = re.sub(r'[\\/:*?"<>|]', '', name)
    name = name.strip().replace('  ', ' ')
    return name[:max_len] or "video"


def cleanup_later(path, delay=120):
    """Supprime un fichier temporaire après `delay` secondes."""
    def _rm():
        time.sleep(delay)
        try:
            os.unlink(path)
            os.rmdir(os.path.dirname(path))
        except Exception:
            pass
    threading.Thread(target=_rm, daemon=True).start()


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/api/info")
def get_info():
    """
    GET /api/info?url=<youtube_url>
    Retourne les métadonnées de la vidéo (titre, durée, miniature).
    """
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL manquante"}), 400

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Choisir la meilleure miniature (la plus grande)
        thumbnails = info.get("thumbnails", [])
        thumbnail  = info.get("thumbnail", "")
        if thumbnails:
            best = max(thumbnails, key=lambda t: t.get("width", 0) * t.get("height", 0))
            thumbnail = best.get("url", thumbnail)

        return jsonify({
            "title":     info.get("title", "Vidéo YouTube"),
            "duration":  format_duration(info.get("duration")),
            "thumbnail": thumbnail,
            "uploader":  info.get("uploader", ""),
            "view_count": info.get("view_count", 0),
        })

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private video" in msg or "This video is private" in msg:
            return jsonify({"error": "Vidéo privée ou indisponible"}), 400
        if "Sign in" in msg or "age" in msg.lower():
            return jsonify({"error": "Vidéo restreinte par l'âge ou nécessite une connexion"}), 400
        return jsonify({"error": "Impossible de récupérer les informations de cette vidéo"}), 400
    except Exception as e:
        return jsonify({"error": f"Erreur inattendue : {str(e)}"}), 500


@app.route("/api/download")
def download():
    """
    GET /api/download?url=<youtube_url>&format=mp4|mp3
    Télécharge la vidéo et renvoie le fichier en streaming.
    """
    url = request.args.get("url", "").strip()
    fmt = request.args.get("format", "mp4").lower()

    if not url:
        return jsonify({"error": "URL manquante"}), 400
    if fmt not in ("mp3", "mp4"):
        return jsonify({"error": "Format invalide — utilisez mp3 ou mp4"}), 400

    tmpdir = tempfile.mkdtemp(prefix="tubegrab_")

    # Détecter si ffmpeg est disponible
    import shutil
    has_ffmpeg = shutil.which("ffmpeg") is not None

    # Configuration yt-dlp optimisée pour YouTube
    base_opts = {
        "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_args": {"youtube": {"player_client": ["android_vr", "web"]}},
    }

    if fmt == "mp3":
        if has_ffmpeg:
            # ffmpeg disponible : extraction audio + conversion MP3
            opts = {**base_opts,
                "format": "bestaudio[ext=m4a]/bestaudio",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            }
        else:
            # Sans ffmpeg : m4a natif (qualité équivalente)
            opts = {**base_opts, "format": "bestaudio[ext=m4a]/bestaudio"}
    else:
        if has_ffmpeg:
            # ffmpeg disponible : fusion meilleure vidéo + meilleur audio → 1080p+
            opts = {**base_opts,
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
            }
        else:
            # Sans ffmpeg : format pré-muxé MP4 (360p, aucune dépendance)
            opts = {**base_opts, "format": "best[ext=mp4]/best"}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = safe_filename(info.get("title", "video"))

        # Trouver le fichier téléchargé (n'importe quelle extension)
        files = [f for f in glob.glob(os.path.join(tmpdir, "*")) if os.path.isfile(f)]
        if not files:
            return jsonify({"error": "Fichier introuvable après téléchargement"}), 500

        filepath = files[0]
        actual_ext = os.path.splitext(filepath)[1].lstrip(".")
        download_name = f"{title}.{actual_ext}"

        if actual_ext in ("m4a", "aac", "ogg", "opus", "webm") and fmt == "mp3":
            mime = "audio/mp4"
        elif actual_ext == "mp4":
            mime = "video/mp4"
        else:
            mime = "application/octet-stream"

        cleanup_later(filepath, delay=120)

        return send_file(
            filepath,
            as_attachment=True,
            download_name=download_name,
            mimetype=mime,
        )

    except yt_dlp.utils.DownloadError as e:
        # Retourner le vrai message d'erreur yt-dlp pour faciliter le débogage
        raw = str(e).replace("ERROR: ", "").strip()
        return jsonify({"error": raw[:300]}), 400
    except Exception as e:
        return jsonify({"error": f"Erreur serveur : {str(e)}"}), 500


# ── Santé du serveur ─────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "service": "TubeGrab API"})


# ── Lancement ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "─" * 50)
    print("  TubeGrab Backend — démarré")
    print("  API disponible sur : http://localhost:5000")
    print("  Ouvrez index.html dans votre navigateur")
    print("─" * 50 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
