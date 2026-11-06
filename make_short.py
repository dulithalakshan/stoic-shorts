"""Generate one stoic/discipline YouTube Short and (optionally) upload it.

Pipeline: Gemini script -> edge-tts voice + word timings -> Pexels portrait
clips -> FFmpeg 1080x1920 video with burned-in captions -> YouTube upload.

Env vars:
  GEMINI_API_KEY        (optional; falls back to built-in scripts)
  PEXELS_API_KEY        (optional; stock footage)
  PIXABAY_API_KEY       (optional; stock footage. With neither key: dark animated background)
  YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN   (needed for --upload)
  YT_PRIVACY            public | unlisted | private   (default: public)
  GEMINI_MODEL          default: gemini-flash-latest (auto-falls back)
  TTS_VOICE             default: en-US-ChristopherNeural
"""
import argparse, asyncio, json, os, random, re, subprocess, sys, datetime, pathlib, shutil

import requests

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "out"
HISTORY = ROOT / "history.json"
W, H = 1080, 1920
VOICE = os.getenv("TTS_VOICE", "en-US-ChristopherNeural")
FONT = os.getenv("CAPTION_FONT", "DejaVu Sans")

FALLBACK_SCRIPTS = [
    {
        "topic": "discipline over motivation",
        "script": "Motivation is a guest. It visits when it wants and leaves without warning. "
                  "Discipline lives with you. It wakes you up when the room is cold and the bed is warm. "
                  "Stop waiting to feel ready. Marcus Aurelius ruled an empire and still had to talk himself out of bed. "
                  "Get up. Do the work. Feelings follow action.",
        "title": "Discipline Doesn't Wait For Motivation",
        "keywords": ["dark mountain sunrise", "man running rain", "stormy ocean", "lonely road night"],
    },
    {
        "topic": "control what you can",
        "script": "You cannot control the storm. You cannot control what they say, or what tomorrow brings. "
                  "Epictetus was a slave, and still he was free, because he knew one thing. "
                  "Your mind is yours. Your effort is yours. Your response is yours. "
                  "Let the rest go. Master yourself, and nothing can master you.",
        "title": "The Only Thing You Truly Control",
        "keywords": ["storm clouds", "waves crashing rocks", "man silhouette sunset", "foggy forest"],
    },
    {
        "topic": "embrace hardship",
        "script": "The obstacle is not in the way. The obstacle is the way. "
                  "Every hard day is training you for a harder one. "
                  "Fire tests gold, and adversity tests strong men. "
                  "So stop asking for an easier life. Ask to become someone who can carry it.",
        "title": "The Obstacle Is The Way",
        "keywords": ["fire flames dark", "boxer training", "mountain climbing", "heavy rain city night"],
    },
]


# ---------------------------------------------------------------- script ----
def load_history():
    if HISTORY.exists():
        return json.loads(HISTORY.read_text())
    return []


def gemini_script(history):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    model = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    recent = [h["topic"] for h in history[-60:]]
    prompt = f"""You write scripts for a YouTube Shorts channel about stoicism and discipline.
Tone: calm, deep, serious, masculine-neutral, no cringe, no emojis, no hashtags in the script.
Write ONE original voiceover script of 55-75 words (about 22-28 seconds spoken).
Open with a strong hook line. Use short punchy sentences. You may reference a real stoic
(Marcus Aurelius, Seneca, Epictetus) but do not invent fake quotes; paraphrase ideas instead.
End with a clear call to action line (not "subscribe").
Avoid these recently used topics: {recent}

Return JSON with keys:
 "topic": 3-6 word topic label,
 "script": the voiceover text,
 "title": catchy YouTube title under 60 characters, no hashtags,
 "description": 1-2 sentence description,
 "tags": list of 8 short tags,
 "keywords": list of 4 short stock-footage search phrases (cinematic, moody, no faces needed)."""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
    }
    base = "https://generativelanguage.googleapis.com/v1beta"
    candidates = [model, "gemini-flash-latest", "gemini-flash-lite-latest"]
    try:  # also discover whatever flash models this key can use right now
        lst = requests.get(f"{base}/models", params={"key": key, "pageSize": 200}, timeout=30).json()
        found = [m["name"].split("/", 1)[1] for m in lst.get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])
                 and "flash" in m["name"] and not re.search(r"image|tts|audio|live|exp|preview", m["name"])]
        candidates += sorted(found, reverse=True)
    except Exception as e:  # noqa: BLE001
        print("Gemini model list failed:", e, file=sys.stderr)
    for m in dict.fromkeys(candidates):
        try:
            r = requests.post(f"{base}/models/{m}:generateContent", params={"key": key}, json=body, timeout=60)
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            assert 30 <= len(data["script"].split()) <= 95
            print("Script by", m)
            return data
        except Exception as e:  # noqa: BLE001
            print(f"Gemini {m} failed: {str(e).split('?')[0]}", file=sys.stderr)
    print("All Gemini models failed, using fallback script", file=sys.stderr)
    return None


def pick_script(history):
    data = gemini_script(history)
    if data:
        return data
    used = {h["topic"] for h in history}
    pool = [s for s in FALLBACK_SCRIPTS if s["topic"] not in used] or FALLBACK_SCRIPTS
    return dict(random.choice(pool))


# ----------------------------------------------------------------- voice ----
async def _tts(text, mp3_path):
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE, rate="-6%", pitch="-4Hz", boundary="WordBoundary")
    words = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append({"text": chunk["text"], "start": start,
                              "end": start + chunk["duration"] / 1e7})
    return words


def make_voice(text, workdir):
    mp3 = workdir / "voice.mp3"
    words = asyncio.run(_tts(text, mp3))
    return mp3, words


def duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)])
    return float(out)


# -------------------------------------------------------------- captions ----
def ass_time(t):
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def make_captions(words, path, per_line=3):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},86,&H00FFFFFF,&H0000D7FF,&H00000000,&H64000000,1,0,0,0,100,100,1,0,1,6,3,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    i = 0
    while i < len(words):
        group = words[i:i + per_line]
        # break groups at sentence ends
        for j, w in enumerate(group):
            if re.search(r"[.!?]$", w["text"]) and j < len(group) - 1:
                group = group[:j + 1]
                break
        start = group[0]["start"]
        end = words[i + len(group)]["start"] if i + len(group) < len(words) else group[-1]["end"] + 0.4
        # highlight current word in gold
        for k, w in enumerate(group):
            ws = w["start"]
            we = group[k + 1]["start"] if k + 1 < len(group) else end
            parts = []
            for m, x in enumerate(group):
                t = x["text"].upper().replace("{", "").replace("}", "")
                parts.append(r"{\c&H0000D7FF&}" + t + r"{\c&H00FFFFFF&}" if m == k else t)
            lines.append(f"Dialogue: 0,{ass_time(ws)},{ass_time(we)},Cap,,0,0,0,,{' '.join(parts)}")
        i += len(group)
    path.write_text(header + "\n".join(lines) + "\n")


# ---------------------------------------------------------------- footage ----
def pexels_clips(keywords, n, workdir):
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        return []
    clips, seen = [], set()
    queries = list(keywords) + ["dark cinematic nature", "stormy sky", "man walking alone"]
    for q in queries:
        if len(clips) >= n:
            break
        try:
            r = requests.get("https://api.pexels.com/videos/search",
                             headers={"Authorization": key},
                             params={"query": q, "orientation": "portrait", "per_page": 15},
                             timeout=30)
            r.raise_for_status()
            vids = [v for v in r.json().get("videos", []) if v["id"] not in seen and v["duration"] >= 5]
            random.shuffle(vids)
            for v in vids[:1]:
                files = [f for f in v["video_files"] if f.get("height") and f["height"] >= 1280
                         and f.get("width", 0) < f["height"] and f["file_type"] == "video/mp4"]
                if not files:
                    continue
                f = min(files, key=lambda f: f["height"])
                dest = workdir / f"clip{len(clips)}.mp4"
                with requests.get(f["link"], stream=True, timeout=120) as d:
                    d.raise_for_status()
                    with open(dest, "wb") as fh:
                        shutil.copyfileobj(d.raw, fh)
                seen.add(v["id"])
                clips.append(dest)
        except Exception as e:  # noqa: BLE001
            print(f"Pexels '{q}' failed: {e}", file=sys.stderr)
    return clips


def pixabay_clips(keywords, n, workdir, start=0):
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        return []
    clips, seen = [], set()
    queries = list(keywords) + ["dark clouds", "storm ocean", "mountain fog", "night city rain"]
    for q in queries:
        if len(clips) >= n:
            break
        try:
            r = requests.get("https://pixabay.com/api/videos/",
                             params={"key": key, "q": q[:100], "per_page": 20, "safesearch": "true"},
                             timeout=30)
            r.raise_for_status()
            hits = [h for h in r.json().get("hits", []) if h["id"] not in seen and h.get("duration", 0) >= 5]
            random.shuffle(hits)
            for h in hits[:1]:
                v = h["videos"]
                f = next((v[s] for s in ("medium", "large", "small") if v.get(s, {}).get("url")), None)
                if not f:
                    continue
                dest = workdir / f"clip{start + len(clips)}.mp4"
                with requests.get(f["url"], stream=True, timeout=120) as d:
                    d.raise_for_status()
                    with open(dest, "wb") as fh:
                        shutil.copyfileobj(d.raw, fh)
                seen.add(h["id"])
                clips.append(dest)
        except Exception as e:  # noqa: BLE001
            print(f"Pixabay '{q}' failed: {e}", file=sys.stderr)
    return clips


# ----------------------------------------------------------------- render ----
def render(clips, voice, subs, total, out_path, workdir):
    seg = total / max(len(clips), 1)
    vf_grade = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                "setsar=1,fps=30,eq=saturation=0.75:brightness=-0.06:contrast=1.08,"
                "vignette=PI/4")
    parts = []
    if clips:
        for i, c in enumerate(clips):
            p = workdir / f"seg{i}.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(c),
                            "-t", f"{seg:.3f}", "-an", "-vf", vf_grade,
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(p)], check=True)
            parts.append(p)
    else:  # fallback: slow-moving dark gradient + film grain
        p = workdir / "seg0.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", f"gradients=s={W}x{H}:c0=0x05070a:c1=0x2a2f38:c2=0x0b0d10:speed=0.004:d={total:.2f}:r=30",
                        "-vf", "noise=alls=4:allf=t,vignette=PI/4",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(p)], check=True)
        parts.append(p)

    concat = workdir / "concat.txt"
    concat.write_text("".join(f"file '{p.name}'\n" for p in parts))
    joined = workdir / "joined.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                    "-c", "copy", str(joined)], check=True)

    music = sorted((ROOT / "music").glob("*.mp3")) if (ROOT / "music").exists() else []
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-i", str(voice)]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(random.choice(music))]
        afilter = ("[1:a]volume=1.0[v];[2:a]volume=0.12,afade=t=out:st="
                   f"{total - 1.5:.2f}:d=1.5[m];[v][m]amix=inputs=2:duration=first:dropout_transition=0[a]")
    else:  # subtle low drone so it isn't silent between words
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=55:duration={total:.2f}"]
        afilter = (f"[1:a]volume=1.0[v];[2:a]volume=0.05,afade=t=in:d=1,afade=t=out:st={total - 1.5:.2f}:d=1.5[m];"
                   "[v][m]amix=inputs=2:duration=first:dropout_transition=0[a]")
    subs_esc = str(subs).replace(":", r"\:")
    cmd += ["-filter_complex", f"[0:v]ass='{subs_esc}'[vout];{afilter}",
            "-map", "[vout]", "-map", "[a]", "-t", f"{total:.2f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-maxrate", "6M", "-bufsize", "12M", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, check=True)


# ----------------------------------------------------------------- upload ----
def upload(video, meta):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        client_id=os.environ["YT_CLIENT_ID"],
                        client_secret=os.environ["YT_CLIENT_SECRET"],
                        token_uri="https://oauth2.googleapis.com/token",
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    tags = meta.get("tags") or ["stoicism", "discipline", "motivation", "self improvement",
                                "marcus aurelius", "mindset", "shorts", "stoic"]
    desc = (meta.get("description") or meta["topic"].capitalize() + ".") + \
        "\n\n#shorts #stoicism #discipline #motivation"
    body = {
        "snippet": {"title": (meta["title"][:90] + " #shorts"), "description": desc,
                    "tags": tags[:15], "categoryId": "22"},
        "status": {"privacyStatus": os.getenv("YT_PRIVACY", "public"),
                   "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print("Uploaded: https://youtube.com/shorts/" + resp["id"])
    return resp["id"]


# ------------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upload", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    work = OUT / stamp
    work.mkdir()

    history = load_history()
    meta = pick_script(history)
    print("Topic:", meta["topic"])
    print("Script:", meta["script"])

    voice, words = make_voice(meta["script"], work)
    total = duration(voice) + 0.8
    subs = work / "captions.ass"
    make_captions(words, subs)
    n_clips = max(3, min(6, round(total / 5)))
    clips = pexels_clips(meta.get("keywords", []), n_clips, work)
    if len(clips) < n_clips:
        clips += pixabay_clips(meta.get("keywords", []), n_clips - len(clips), work, start=len(clips))
    video = work / "short.mp4"
    render(clips, voice, subs, total, video, work)
    (work / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"Rendered {video} ({total:.1f}s, {len(clips)} clips)")

    vid = upload(video, meta) if args.upload else None
    history.append({"date": stamp, "topic": meta["topic"], "title": meta["title"], "video_id": vid})
    HISTORY.write_text(json.dumps(history, indent=2))


if __name__ == "__main__":
    main()
