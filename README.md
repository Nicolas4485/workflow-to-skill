# workflow-to-skill

Record yourself doing a workflow once, narrating as you go. This plugin turns that recording into a reusable, tested Claude skill.

Claude cannot take video as input. So the plugin extracts two things from the recording, locally on your machine: keyframes (screenshots at the moments something changed) and a timestamped transcript of your narration. Claude reads those, drafts structured workflow notes, takes one round of corrections from you, then uses the skill-creator skill to write and test the final skill.

## Requirements

- ffmpeg and ffprobe on PATH.
  - Windows: `winget install --id Gyan.FFmpeg -e` then close and reopen the terminal.
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`
- Python 3.10 or newer.
- faster-whisper for local transcription: `python -m pip install -r skills/record-to-skill/scripts/requirements.txt`
  The first run downloads the Whisper model (a few hundred MB). After that it works offline.

## Install

Claude Code:

```
/plugin marketplace add Nicolas4485/workflow-to-skill
/plugin install workflow-to-skill@workflow-to-skill
```

Cowork (Claude desktop app): Customize > Plugins > Add marketplace, then paste this repo's GitHub URL. If your build supports uploading a plugin file instead, upload `workflow-to-skill.plugin` from the repo's releases.

## Record your workflow

Record the screen with the microphone ON and narrate every click: what you are clicking, why, and what you expect to happen.

- Windows: Win+G opens Game Bar (record with mic), or use Snipping Tool's screen recording.
- Mac: Cmd+Shift+5, choose a microphone under Options.

## Usage

```
1. Record the workflow with your mic on. Save it, for example: C:\Videos\invoice-flow.mp4
2. In Claude Code or Cowork: /workflow-to-skill:record-to-skill C:\Videos\invoice-flow.mp4
3. Claude extracts frames and transcript, then shows you workflow notes. Correct them once.
4. Tell Claude where the skill should live. skill-creator writes and tests it.
5. Try the suggested test prompt in a fresh session.
```

Saying "turn this recording into a skill" with a path also triggers it.

## Privacy

Everything runs locally: frame extraction, audio extraction, and transcription. Nothing is uploaded anywhere. The one exception: passing `--cloud` to extract.py sends the audio track (only the audio) to OpenAI for transcription, and the script prints a warning when it does.

## Known limits

- Fast clicks can fall between keyframes. Narrate every click so the transcript covers what the frames miss.
- If the extraction script cannot run inside Cowork (no ffmpeg or Python in that environment), run it yourself in a terminal:
  `python <plugin-folder>/skills/record-to-skill/scripts/extract.py <video>`
  then rerun the skill and give it the `<video-name>_extract` folder as the path.

## Roadmap

- Live capture while you work (screenshot on every click plus mic recording), not built yet.
