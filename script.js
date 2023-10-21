const startRecordButton = document.getElementById('startRecord');
const stopRecordButton = document.getElementById('stopRecord');
const audioPlayer = document.getElementById('audioPlayer');
const downloadLink = document.getElementById('downloadLink');

let mediaRecorder;
let audioChunks = [];
        const recordedAudioList = []; // Array to store recorded audio data and filenames
navigator.mediaDevices.getUserMedia({ audio: true })
    .then(function (stream) {
        mediaRecorder = new MediaRecorder(stream);

        mediaRecorder.ondataavailable = function (event) {
            if (event.data.size > 0) {
                audioChunks.push(event.data);
            }
        };

        // mediaRecorder.onstop = function () {
        //     const audioBlob = new Blob(audioChunks, { type: 'audio/wav' });
        //     const audioUrl = URL.createObjectURL(audioBlob);
        //     audioPlayer.src = audioUrl;
        //     downloadLink.href = audioUrl;
        //     downloadLink.download = 'recorded_audio.wav';
        //     downloadLink.style.display = 'block';
        // };

        // mediaRecorder.onstop = function () {
        //     const audioBlob = new Blob(audioChunks, { type: 'audio/wav' });

        //     // Create a unique filename based on the current timestamp
        //     const filename = `recording_${Date.now()}.wav`;

        //     const audioUrl = URL.createObjectURL(audioBlob);

        //     // Create a download link for each recording
        //     const downloadLink = document.createElement('a');
        //     downloadLink.href = audioUrl;
        //     downloadLink.download = filename;
        //     downloadLink.style.display = 'block';

        //     // Add the download link to the list
        //     recordedAudioList.push({ filename, downloadLink });

        //     // Create a list item for each recording
        //     const listItem = document.createElement('li');
        //     listItem.textContent = filename;
        //     listItem.appendChild(downloadLink);

        //     // Add the list item to a list (e.g., an <ul> element with id "recordingsList")
        //     const recordingsList = document.getElementById('recordingsList');
        //     recordingsList.appendChild(listItem);
        // };



mediaRecorder.onstop = function () {
    const audioBlob = new Blob(audioChunks, { type: 'audio/wav' });
    const filename = `recording_${Date.now()}.wav`;
    recordedAudioList.push({ filename, audioBlob, });
    // Rest of your code for handling the download links
};

        startRecordButton.addEventListener('click', () => {
            mediaRecorder.start();
            startRecordButton.disabled = true;
            stopRecordButton.disabled = false;
        });

        stopRecordButton.addEventListener('click', () => {
            mediaRecorder.stop();
            startRecordButton.disabled = false;
            stopRecordButton.disabled = true;
        });
    })
    .catch(function (error) {
        console.error('Error accessing the microphone:', error);
    });

    const createZipButton = document.getElementById('downloadLink');

createZipButton.addEventListener('click', () => {
    const zip = new JSZip();
    
    // Loop through recordedAudioList and add each audioBlob to the zip file.
    recordedAudioList.forEach((item) => {
        zip.file(item.filename, item.audioBlob);
    });

    // Generate the zip file.
    zip.generateAsync({ type: 'blob' }).then((zipBlob) => {
        // Create a download link for the zip file.
        const zipUrl = URL.createObjectURL(zipBlob);
        const zipLink = document.createElement('a');
        zipLink.href = zipUrl;
        zipLink.download = 'recordings.zip';

        // Trigger the click event to prompt the user to download the zip file.
        zipLink.click();
    });
});


function getSelectedTextRange() {
    const paragraph = document.getElementById("paragraph");

    if (window.getSelection) {
        const selection = window.getSelection();

        if (selection.rangeCount > 0) {
            const range = selection.getRangeAt(0);
            const selectedText = range.toString();

            // Display the selected text and its range.
            const selectedTextInfo = `Selected Text: "${selectedText}"<br>Range Start: ${range.startOffset}, Range End: ${range.endOffset}`;
            document.getElementById("selectedText").innerHTML = selectedTextInfo;
        } else {
            document.getElementById("selectedText").innerHTML = "No text selected.";
        }
    }
}






