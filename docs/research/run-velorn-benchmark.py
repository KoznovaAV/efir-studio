"""Automate the common media fixture through Velorn's local editing MCP."""
import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess
import sys
from time import perf_counter, sleep
from urllib.request import Request, urlopen


ENDPOINT = "http://127.0.0.1:19790/mcp"


class Velorn:
    def __init__(self):
        self.calls = Counter()

    def call(self, tool, **arguments):
        self.calls[tool] += 1
        payload = {"jsonrpc": "2.0", "id": sum(self.calls.values()), "method": "tools/call",
                   "params": {"name": tool, "arguments": arguments}}
        request = Request(ENDPOINT, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        with urlopen(request, timeout=45) as response:
            result = json.load(response)
        if "error" in result:
            raise RuntimeError(f"{tool}: {result['error']['message']}")
        content = result["result"]
        value = json.loads(content["content"][0]["text"])
        if content.get("isError"):
            raise RuntimeError(f"{tool}: {value}")
        return value

    def apply(self, tool, **arguments):
        preview = self.call(tool, previewOnly=True, **arguments)
        suggestion = preview.get("suggestedApplyCall", {})
        result = self.call(tool, **suggestion.get("arguments", {**arguments, "previewOnly": False}))
        if result.get("success") is False:
            raise RuntimeError(f"{tool}: {result}")
        return result.get("result", result)

    def import_media(self, source):
        return self.apply("import_asset_from_path", path=str(source), asSequence=False)["asset"]["id"]

    def add(self, asset, track, start, duration):
        self.apply("add_asset_to_timeline", assetId=asset, trackId=track, startSeconds=start,
                   durationSeconds=duration, includeAudio=False, resolveOverlaps=False)

    def export(self, folder, name, duration):
        path = folder / f"{name}-av.mp4"
        start = perf_counter()
        result = self.apply("export_timeline", format="mp4", videoCodec="h264", audioCodec="aac",
                            useHardwareEncoder=False, preset="veryfast", qualityMode="crf", crf=19,
                            includeAudio=True, audioBitrateKbps=192, audioSampleRate=48000,
                            audioChannels=2, normalizeAudio=False, resolution="project", fps=25,
                            range="custom", rangeStart=0, rangeEnd=duration, outputPath=str(path))
        print(f"{name} accepted: {result.get('jobId')}", file=sys.stderr, flush=True)
        job_id = result.get("jobId") or result.get("nativeJobId")
        if not job_id:
            raise RuntimeError(f"Export response lacks a job ID: {result}")
        deadline = perf_counter() + 900
        while perf_counter() < deadline:
            status = self.call("get_export_status", jobId=job_id)
            job = status.get("result", status).get("job") or {}
            state = job.get("status")
            if state == "completed" and path.is_file() and path.stat().st_size:
                (folder / f"{name}-job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2),
                                                        encoding="utf-8")
                return path, perf_counter() - start
            if state in {"failed", "error", "cancelled", "canceled"}:
                raise RuntimeError(f"{name}: {status}")
            sleep(0.5)
        raise TimeoutError(f"{name}: did not finish before the deadline")


def timecode(frames):
    seconds, frames = divmod(frames, 25)
    return f"00:{seconds // 60:02}:{seconds % 60:02},{frames * 40:03}"


def render(velorn, ffmpeg, output, name, scenes):
    position = 0
    cues = []
    for number, (_, _, frames, caption) in enumerate(scenes, 1):
        end = position + frames
        cues.append(f"{number}\n{timecode(position)} --> {timecode(end)}\n{caption}\n")
        position = end
    srt = output / f"{name}.srt"
    srt.write_text("\n".join(cues), encoding="utf-8")
    media, export_seconds = velorn.export(output, name, position / 25)
    delivery = output / f"{name}.mp4"
    started = perf_counter()
    subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-i", str(media),
                    "-i", str(srt), "-map", "0:v", "-map", "0:a", "-map", "1:s",
                    "-c", "copy", "-c:s", "mov_text", "-movflags", "+faststart", str(delivery)],
                   check=True, timeout=60)
    result = {"operation": name, "export_seconds": round(export_seconds, 3),
              "caption_mux_seconds": round(perf_counter() - started, 3),
              "bytes": delivery.stat().st_size}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def retime(velorn, scenes):
    position = 0
    clips = []
    for visual, voice, frames, _ in scenes:
        clips.extend({"clipId": clip, "startSeconds": position / 25, "durationSeconds": frames / 25,
                      "trimStartSeconds": 0, "trimEndSeconds": frames / 25} for clip in (visual, voice))
        position += frames
    velorn.apply("trim_clips", clips=clips)
    velorn.apply("save_project")


def resolves_locally(assets, folder):
    return len(assets) == 13 and all(
        Path(item["absolutePath"]).resolve().is_relative_to(folder)
        and Path(item["absolutePath"]).is_file()
        for item in assets
    )


def render_copy(velorn, ffmpeg, output, source, scenes):
    copied = output / "Перенос Velorn"
    parked = source.with_name(f"{source.name}-audit-parked")
    if parked.exists():
        raise FileExistsError(parked)
    shutil.copytree(source, copied)
    velorn.apply("open_project", projectPath=str(copied))
    assets = velorn.call("get_assets")["assets"]
    independent_with_source_present = resolves_locally(assets, copied)
    if independent_with_source_present:
        return render(velorn, ffmpeg, output, "copy", scenes), True

    # Switch away from the copied project before parking the test-created source.
    velorn.apply("create_project", name=f"{source.name}-audit-switch", width=1080, height=1920, fps=25)
    source.rename(parked)
    try:
        velorn.apply("open_project", projectPath=str(copied))
        if not resolves_locally(velorn.call("get_assets")["assets"], copied):
            raise RuntimeError("Copied project contains missing or externally linked imported assets")
        return render(velorn, ffmpeg, output, "copy", scenes), False
    finally:
        parked.rename(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--projects-root", type=Path, required=True)
    parser.add_argument("--project", default="EFIR benchmark 96s")
    args = parser.parse_args()
    fixture, output = args.fixture.resolve(), args.output.resolve()
    projects = args.projects_root.resolve(strict=True)
    source = projects / args.project
    if source.resolve().parent != projects or source.exists() or output.is_relative_to(source):
        parser.error("Use a new project name in the configured Velorn projects folder and a separate output.")
    ffmpeg = args.ffmpeg.resolve()
    output.mkdir(parents=True)
    velorn = Velorn()
    started = perf_counter()
    settings = {"name": args.project, "width": 1080, "height": 1920, "fps": 25}
    preview = velorn.call("create_project", previewOnly=True, **settings)
    if Path(preview["result"]["plan"]["targetPath"]).resolve() != source:
        parser.error("The projects folder in Velorn differs from --projects-root.")
    project = velorn.apply("create_project", **settings)["project"]
    if Path(project["path"]).resolve() != source:
        raise RuntimeError("Velorn returned an unexpected project folder.")
    timeline = velorn.call("get_timeline")
    tracks = {track["type"]: track["id"] for track in timeline["tracks"]}
    captions = [block.splitlines()[2] for block in
                (fixture / "captions.srt").read_text(encoding="utf-8").strip().split("\n\n")]
    for number in range(1, 7):
        for kind, source in (("video", f"clip-{number}.mp4"), ("audio", f"mix-{number}.wav")):
            asset = velorn.import_media(fixture / "media" / source)
            velorn.add(asset, tracks[kind], (number - 1) * 16, 16)
    velorn.apply("save_project")
    setup_seconds = perf_counter() - started
    clips = {clip["name"]: clip["id"] for clip in velorn.call("get_timeline")["clips"]}
    scenes = [(clips[f"clip-{n}.mp4"], clips[f"mix-{n}.wav"], 400, captions[n - 1])
              for n in range(1, 7)]
    results = [render(velorn, ffmpeg, output, "cold", scenes)]
    results.append(render(velorn, ffmpeg, output, "warm", scenes))
    visual, voice, _, caption = scenes[1]
    replacement = velorn.import_media(fixture / "media/clip-7.mp4")
    velorn.apply("replace_clip_with_asset", clipId=visual, replacementAssetId=replacement)
    velorn.apply("save_project")
    results.append(render(velorn, ffmpeg, output, "replace", scenes))
    scenes[1] = (visual, voice, 388, caption)
    retime(velorn, scenes)
    results.append(render(velorn, ffmpeg, output, "duration", scenes))
    scenes.pop(1)
    scenes.insert(3, (visual, voice, 388, "Контрольная правка русских субтитров."))
    retime(velorn, scenes)
    results.append(render(velorn, ffmpeg, output, "order-caption", scenes))
    copy_result, independent = render_copy(velorn, ffmpeg, output, source, scenes)
    results.append(copy_result)
    result = {"project": project["path"], "setup_seconds": round(setup_seconds, 3),
              "copy_used_its_own_assets_while_source_exists": independent,
              "mcp_calls": velorn.calls, "results": results}
    report = json.dumps(result, ensure_ascii=False, indent=2)
    (output / "results.json").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
