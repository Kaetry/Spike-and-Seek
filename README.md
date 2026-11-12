# Spike-and-Seek
## Multimodal datastream cross-examination tool

This app is meant for finding the same events on different data streams assuming all the datastreams were recorded within the same experimental context.

## Use cases: 
- annotation of animal vocalizations on audio recorder data (establishing the caller's identity) using data from multiple video cameras *(Available in the current version)*
- establishing the possible reasons of elevated heart rate of an animal by checking the data from audio recorder for loud foreign noises *(Not available yet)*
- etc.

Originally, the app was developed for a specific project focused on investigating of correlation between vocal behaviour, and breathing of individually marked animals housed as a group.

## Before you start 

Be warned that will require some preparations to work (see below). In the end, you will be able to input a name of a file that contains the event of interest in a datastream A, and the event's timestamp in the file, and the app returns the exact filename containing the recording of the event and its timestamp in a datastream B.

The app is expecting certain filename convention: unique name per datastream, plus trailing index for consecutive files, if there is more than one file for this datastream (*filename_index.extension*).

**Supported formats**

- **Video**: `.3gp`, `.avi`, `.flv`, `.m2ts`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.mpeg`, `.mpg`, `.mts`, `.ogv`, `.ts`, `.vob`, `.webm`, `.wmv`

- **Audio**: `.wav`, `.flac`

### Example

> We record two puppies having a playdate (let's say, later, we want to know which puppy barked the most). We have one audio recorder and two video cameras. Audio recorder works from the beginning to the end of the playdate. Video cameras write files in 30 minute chunks. We turn the recording devices on one one by one.
>
> To be good to use with `Spike-and-Seek`, the filenames should look as follows:
>
> - `playdate.wav`
> - `Alice-VideoCamera_1.mp4`
> - `Alice-VideoCamera_2.mp4`
> - `Alice-VideoCamera_3.mp4`
> - `VideoCamera2_1.mp4`
> - `VideoCamera2_2.mp4`
> 
> Then, in further analysis, the data stream names will be `playdate` (audio, 1 file), `Alice-VideoCamera` (video, 3 files), and `VideoCamera2` (video, 2 files).

After you started working, don't rename the files and don't move them around, the app doesn't track the changes in your file system.

## How to start

First, we need to synchronize the data streams. For that, you will need to annotate the same event in all the datastreams that you want to cross-inspect later. If we are mapping a bundle of audio and video data, it will be the sound recorded by both the audio recorder and the video camera's mic (think 🎬 clapperboard in movie production).

You need to do it once, then the app will generate a reference CSV table to look up any time you would like to continue working with this data bundle.

0. [Download Praat](https://www.fon.hum.uva.nl/praat/)

1. Launch `Spike-and-Seek` and choose data directory.

> ### Requirements and launch
>
> 1. Install Python dependencies from the repository folder:
>
>   ```powershell
>   python -m pip install -r requirements.txt
>   ```
>
> 2. Launch the application:
>
>   ```powershell
>   python Spike_and_Seek.py
>   ```

2. If you are lacking the synchronization reference table, the app will offer you to generate one. 

    For that, you need to extract soundtracks from the videos that contain the "clapperboard" synchronization event (if you don't know which one to pick and have enough space on your drive, you can just extract soundtracks from all the videos).

3. Annotate "the clapperboard event" in Praat TextGrid. [Here is the manual on how it is done.](https://www.fon.hum.uva.nl/praat/manual/Intro_7__Annotation.html)

    Some key things: create a Tier named `sync` (it can be either point or interval), and annotate "the clapperboard event" for each respective sound file. The text of the annotation must match the WAV filename, and for each of the filenames there should be exactly one annotation.

> #### Back to our example
> 
> While inspecting the `playdate.WAV` audiofile, we heard the sound of the closing door when the experimenter left the room and left the puppies alone in the very beginning of the playdate. We know that both video cameras were already running when that happened. So, we extract soundtracks from `Alice-VideoCamera_1.mp4` and `VideoCamera2_1.mp4` as WAV files.
>
> We open `playdate.WAV`, `Alice-VideoCamera_1.WAV`and `VideoCamera2_1.WAV`. We create a TextGrid object with a single point tier called `sync`. Then, we open `playdate.WAV` together with this TextGrid and inspect the spectrogram. We find the sound of the closing door and create a point annotation with the text *playdate*. Then, we do the same for `Alice-VideoCamera_1.WAV`and `VideoCamera2_1.WAV`, creating annotations with text *Alice-VideoCamera_1* and *VideoCamera2_1*.
>
> We save the TextGrid file in the directory together with our data.

4. Back to `Spike-and-Seek`. 

    In the synchronization generation window, browse for the TextGrid we just created. Give the app a moment to do the necessary calculations.

And you are good to go!

## Converting a timestamp

After datastream synchronization:

5. Select the `source` and `target` datastreams: 
    
    the datastream where you spotted the event will be your `source`, and the datastream you want to check will be your `target`.

6. Enter the source time:
   - **Audio source:** seconds from the start of the audio file.
   - **Video source:** the video file's index and a file-local time in
     `HH:MM:SS` or `HH:MM:SS.sss` format.
7. Choose **Calculate** or press Enter.

For an audio `target`, you will get the result is in seconds from the start of the `target` datastream. 

For a video `target`, the app displays the `target` filename and
file-local time. You can press **Go to VLC** to inspect the videofile immediately; playback starts two seconds before the event (so you have time to focus), or at the beginning of the file if the event is within its first two seconds.

## More info

For tecnhical details of the script, check the Script architecture document.

## Contact

To report errors and ask for new features: goncharova.maria.v@gmail.com

