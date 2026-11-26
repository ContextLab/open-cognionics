# Running a session with the headset

These are the steps to run one brain-mapper session on this Mac with the real headset, from a cold
start. Every command is run in a Terminal window, in the folder of this repository. The commands
and options come from `uv run brain-mapper --help` and from the "brain-mapper" page of these docs.

## 1. Before the participant arrives

1. Plug the headset's USB dongle into the Mac and switch the headset on.
2. Prepare the checkout (the first time, or after pulling new code). It prints one `PASS` or `FAIL`
   line per step and changes nothing that is already in place:

```bash
./setup.sh
```

3. Run the start-up checks. They open no serial port and write nothing to the headset:

```bash
uv run brain-mapper --check
```

    Every line should start with `PASS`. The `dongle` line lists the headset dongles found, for
    example `1 headset dongle(s) found: /dev/cu.usbserial-XXXX.` (your port name will differ).
    The `video_host` line says whether the Vimeo player can be reached: the video round needs the
    internet.

## 2. Start the app

With one dongle plugged in, brain-mapper finds it by itself:

```bash
uv run brain-mapper
```

With more than one dongle, name the port that the `dongle` line listed:

```bash
uv run brain-mapper --port /dev/cu.usbserial-XXXX
```

The command prints `brain-mapper at http://127.0.0.1:<port>` (the port is 8740 unless it is
taken) and opens the participant page in the default browser. Leave the Terminal window open
for the whole session.

## 3. The onlooker window

To watch the session on a second window (live traces, contact states, the current round), open
this address in another browser window, with the port the command printed:

```text
http://127.0.0.1:8740/onlooker
```

The onlooker window has no buttons and changes nothing in the session. Closing or reloading it
is safe.

## 4. The session

The default session plays these rounds after the setup, in this order (block times from
`brain_mapper/flow.py`): Words 2 lists of 145 s, Perception 2 blocks of 60 s, 2-back 2 x 50 s,
Flanker 2 x 50 s, Spatial memory 2 x 60 s, Video 165 s, Rest with eyes closed 180 s, Rest with
eyes open 180 s, Delayed free recall 125 s. The blocks add up to 1380 s (23 minutes); the
calibration adds about a minute after the contact check (signal check 6 s, eyes open 20 s, eyes
closed 20 s, blinks), and the instructions, practice and between screens come on
top, so a whole session takes longer than 23 minutes. Words, Video and Delayed recall can end
before their time ("I can't remember any more", the end of the video).

1. On the start screen press Start (the page goes full screen). To play only some rounds, click
   a round, or press Choose several and check the rounds, before pressing Start.
2. Type the participant ID. If this ID has an unfinished earlier session, the page offers to
   continue from the round where it stopped. A session zip saved earlier can also be loaded on
   this screen, to start a new session of the same person.
3. Device setup: wait until the link is live, then Continue.
4. Calibration:
   - Contact check: the 3-D head shows each electrode as Good, Weak or No contact, measured with
     the headset's contact-check tone switched on. If the screen says that the contact-check tone
     is not showing on any electrode, the contact states may be wrong: check the ear clips (the
     reference and ground), then press Check again.
   - Continue switches the tone off; after a short wait the signal check looks at about 6 s of
     the signal itself with the tone off and names each channel Fine, Noisy, Clipped or Flat.
     A channel that is not Fine: adjust that electrode and press Check again (both checks run
     again), or Continue to record with it as it is. When no tone was seen in the contact check,
     the signal check is the only check of the signal itself.
   - Eyes open (look at the cross), eyes closed (a tone says when to open them), blinks.
5. Each F/J task shows its instructions and practice, then "Put your fingers on F and J. Press
   Enter to start." before the practice and before every block; the first trial comes 2 s after
   Enter. After every trial the screen shows Correct, Incorrect or No response and the points in
   the header go up. The last F or J pressed in a trial's response window is the answer.
6. Words: Enter starts each list; after the words, type the words remembered. "I can't remember
   any more" (Tab, then Enter, then Yes) ends the recall early. The same in Delayed free recall.
7. Rests: an instruction screen and Start; a tone starts the rest and a tone ends it (eyes
   closed: close your eyes at the first tone, open them at the second).
8. Video: press Start; the video earns 1 point for each full 10 s watched.
9. At the end the summary shows the points of each round and where the zip was saved; after
   the zip, "See your results" opens the results computed from this session's recording.

## 5. If the session is paused

Leaving full screen, a reload of the page or a lost headset link pauses the session. A pause
ends the block it interrupted: that block is not repeated, its trials stay in the data and the
scores, and Resume goes to the between screen and then the next block (with its instructions and
the Enter start). A pause in the last block of a round goes on with the next round. A rest that
is paused starts again from its own start screen.

The paused screen has "Check the headset": it opens the same contact check as the calibration
(the tone on), then Done switches the tone off and the signal check runs (Resume is grayed out
until it has a result, a few seconds). The result line ("Signal check: ...") stays on the paused
screen. If a channel is not fine, adjust it and press "Check the headset" again, or Resume.

If the link drops during a round, the page says that the headset connection was lost. Nothing
recorded so far is lost: the recording up to the drop is kept, and brain-mapper keeps trying to
reconnect every 2 seconds. This also happens when the dongle stays plugged in: if the headset is
switched off, runs flat or is out of range, the round pauses after about 3 seconds without data.

1. Check that the headset is switched on and near the dongle, and that the dongle is plugged in.
2. Wait for the link to come back (the onlooker window shows the link state).
3. Then press Resume on the participant page. Until the link is back, Resume is grayed out
   and "Waiting for the headset" is shown under it. The session never resumes by itself: someone
   has to press Resume.

"Stop here" on the paused screen ends the session with what was recorded.

If the app itself was stopped, start it again with the same command, type the same participant
ID, and accept the offer to continue. The continued session has a new recording, and the earlier
data stay as they are. Where it starts depends on the round that was interrupted:

- If none of that round's blocks had been played, the round is played again from its start.
  A block ended by a pause counts as played; a block cut off by the app stopping does not.
- If at least one of its blocks had been played, the round counts as done: the continued session
  starts with the next round, and the rest of the interrupted round's blocks are not played.

## 6. Stopping

- To end a session early, use "Stop here" on the participant page and confirm.
- To stop the app, press Ctrl-C in the Terminal window. A session still under way is ended and
  zipped before the app exits; wait for the prompt to return.

## 7. Where the data are

Sessions are kept in `~/brain-mapper/sessions/` (the default of `--data-dir`). Each session has
a folder named `<participant ID>_<YYYYMMDD>_<HHMMSS>` and, beside it, the zip of that folder with
the same name and `.zip`. The summary screen shows the zip's name and folder, with a Download
button. Nothing under a session folder is ever deleted by brain-mapper. If a page sends events
for a session after its zip was made, they are added to its folder and, when the app next stops or
starts, an additional zip `<folder>-late.zip` is made beside the first one.
