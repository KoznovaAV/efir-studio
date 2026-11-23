"""Render the common fixture with MLT shipped in Kdenlive Standalone."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
from time import perf_counter
import xml.etree.ElementTree as ET


FPS = 25


def property_value(node, name, value):
    ET.SubElement(node, "property", name=name).text = str(value)


def make_timeline(scenes, destination):
    end = sum(frames for _, _, frames in scenes) - 1
    mlt = ET.Element("mlt", producer="timeline")
    ET.SubElement(mlt, "profile", width="1080", height="1920", progressive="1",
                  frame_rate_num="25", frame_rate_den="1", sample_aspect_num="1",
                  sample_aspect_den="1", display_aspect_num="9", display_aspect_den="16",
                  colorspace="709")
    for kind, column in (("video", 0), ("audio", 1)):
        for index, scene in enumerate(scenes):
            chain = ET.SubElement(mlt, "chain", id=f"{kind}{index}")
            property_value(chain, "resource", scene[column].as_posix())
            property_value(chain, "mlt_service", "avformat")
        playlist = ET.SubElement(mlt, "playlist", id=f"{kind}-track")
        for index, (_, _, frames) in enumerate(scenes):
            ET.SubElement(playlist, "entry", {"producer": f"{kind}{index}", "in": "0", "out": str(frames - 1)})
    tractor = ET.SubElement(mlt, "tractor", {"id": "timeline", "in": "0", "out": str(end)})
    ET.SubElement(tractor, "track", producer="video-track", hide="audio")
    ET.SubElement(tractor, "track", producer="audio-track", hide="video")
    mix = ET.SubElement(tractor, "transition", {"in": "0", "out": str(end)})
    for name, value in (("mlt_service", "mix"), ("a_track", 0), ("b_track", 1), ("always_active", 1), ("sum", 1)):
        property_value(mix, name, value)
    ET.ElementTree(mlt).write(destination, encoding="utf-8", xml_declaration=True)


def srt_time(frames):
    seconds, fraction = divmod(frames, FPS)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{fraction * 40:03}"


def write_captions(scenes, captions, destination):
    start = 0
    cues = []
    for index, (_, voice, frames) in enumerate(scenes, 1):
        end = start + frames
        cues.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{captions[voice.name]}\n")
        start = end
    destination.write_text("\n".join(cues), encoding="utf-8")


def render(melt, ffmpeg, folder, name, scene_list, captions, prepare=True):
    start = perf_counter()
    timeline, srt = folder / "timeline.mlt", folder / "captions.srt"
    if prepare:
        make_timeline(scene_list, timeline)
        write_captions(scene_list, captions, srt)
    prepare_seconds = perf_counter() - start
    video = folder / f"{name}-av.mp4"
    delivery = folder / f"{name}.mp4"
    start = perf_counter()
    with (folder / f"{name}.log").open("wb") as log:
        subprocess.run([melt, "-silent", timeline.name, "-consumer", f"avformat:{video.name}",
                        "vcodec=libx264", "acodec=aac", "preset=veryfast", "crf=19",
                        "pix_fmt=yuv420p", "bf=0", "ab=192k", "frequency=48000",
                        "channels=2", "threads=4", "real_time=-1", "terminate_on_pause=1"],
                       cwd=folder, stdout=log, stderr=log, check=True, timeout=300)
        mlt_seconds = perf_counter() - start
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", video.name, "-i", srt.name,
                        "-map", "0:v", "-map", "0:a", "-map", "1:s", "-c", "copy", "-c:s", "mov_text",
                        "-movflags", "+faststart", delivery.name], cwd=folder, stdout=log, stderr=log,
                       check=True, timeout=60)
    return {"operation": name, "prepare_seconds": round(prepare_seconds, 3),
            "mlt_seconds": round(mlt_seconds, 3), "total_seconds": round(perf_counter() - start, 3),
            "bytes": delivery.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--melt", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    args = parser.parse_args()
    fixture, output = args.fixture.resolve(), args.output.resolve()
    melt, ffmpeg = args.melt.resolve(), args.ffmpeg.resolve()
    output.mkdir(parents=True)
    for source in (fixture / "media").glob("*"):
        if source.name.startswith(("clip-", "mix-")):
            (output / "media").mkdir(exist_ok=True)
            shutil.copyfile(source, output / "media" / source.name)
    captions = {f"mix-{number}.wav": block.splitlines()[2]
                for number, block in enumerate((fixture / "captions.srt").read_text(
                    encoding="utf-8").strip().split("\n\n"), 1)}
    scenes = [(Path(f"media/clip-{number}.mp4"), Path(f"media/mix-{number}.wav"), 400)
              for number in range(1, 7)]
    results = [render(melt, ffmpeg, output, "cold", scenes, captions)]
    results.append(render(melt, ffmpeg, output, "warm", scenes, captions, prepare=False))
    _, voice, _ = scenes[1]
    replacement = Path("media/clip-7.mp4")
    scenes[1] = (replacement, voice, 400)
    results.append(render(melt, ffmpeg, output, "replace", scenes, captions))
    scenes[1] = (replacement, voice, 388)
    results.append(render(melt, ffmpeg, output, "duration", scenes, captions))
    scenes.insert(3, scenes.pop(1))
    captions[voice.name] = "Контрольная правка русских субтитров."
    results.append(render(melt, ffmpeg, output, "order-caption", scenes, captions))
    copy = output / "Перенос MLT"
    copy.mkdir()
    shutil.copytree(output / "media", copy / "media")
    for name in ("timeline.mlt", "captions.srt"):
        shutil.copyfile(output / name, copy / name)
    results.append(render(melt, ffmpeg, copy, "copy", scenes, captions, prepare=False))
    report = json.dumps(results, ensure_ascii=False, indent=2)
    (output / "results.json").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
