const startStopRecordButton = document.getElementById('startStopRecord');
const downloadLink = document.getElementById('downloadLink');
const createZipButton = document.getElementById('downloadLink');
const recordedAudioList = []; // Array to store recorded audio data and filenames

let recordingOngoing = false;
let mediaRecorder;
let audioChunks = [];
let startTime = 0; // Initialize the start time
let timerInterval; // Variable to store the interval ID

navigator.mediaDevices.getUserMedia({ audio: true })
    .then(function (stream) {
        mediaRecorder = new MediaRecorder(stream);

        mediaRecorder.ondataavailable = function (event) {
            if (event.data.size > 0) {
                audioChunks.push(event.data);
            }
        };

mediaRecorder.onstop = function () {
    const audioBlob = new Blob(audioChunks, { type: 'audio/wav' });
    const filename = `recording_${Date.now()}.wav`;
    recordedAudioList.push({ filename, audioBlob, });
    // Rest of your code for handling the download links
};

/********************************************
 *  EVENT LISTENERS
 ********************************************/

startStopRecordButton.addEventListener('click', () => {
    if (recordingOngoing) {
        /* WAS GOING, NOW STOPPING */
        console.log("Recording is ongoing and button clicked, stop recording, change text to Start");
        mediaRecorder.stop();
        startStopRecordButton.textContent = "Start Recording";
        toggleRecording(); // If true, set it to false.
        stopTimer();
    } else {
        /* WAS NOT GOING, STARTING */
        console.log("Recording is not ongoing and button clicked, start recording, change text to Stop");
        mediaRecorder.start();
        startStopRecordButton.textContent = "Stop Recording";
        toggleRecording(); // If false, set it to true.
        startTimer();
    }
})
})
.catch(function (error) {
console.error('Error accessing the microphone:', error);
});

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

/********************************************
*   FUNCTIONS
********************************************/

// Function to start the timer
function startTimer() {
    startTime = Date.now(); // Record the current time
    timerInterval = setInterval(updateTimer, 100); // Update the timer every 100 milliseconds for more decimal places
}

// Function to stop the timer
function stopTimer() {
    clearInterval(timerInterval); // Stop the timer
    const currentTime = Date.now();
    const elapsedTime = (currentTime - startTime) / 1000; // Calculate elapsed time in seconds with 2 decimal places
    // Log the total time to a text field (you can customize this element by ID)
    document.getElementById('totalTime').textContent = `Total Time: ${elapsedTime.toFixed(2)} seconds`;

    const totalTime = elapsedTime.toFixed(2);

    // Get the existing content of the data field
    const dataField = document.getElementById('dataField');
    const existingContent = dataField.value;

    // Count the number of lines in the existing content
    const numberOfLines = existingContent.split('\n').length;

    // Format numberOfLines with leading zeros (e.g., 001, 025, 194)
    const formattedNumberOfLines = String(numberOfLines).padStart(3, '0');

    // Append the new line with the total time
    const newContent = existingContent + `${formattedNumberOfLines}: ${totalTime} seconds\n`;

    // Update the data field with the new content
    dataField.value = newContent;
}

// Function to update the timer display
function updateTimer() {
    const currentTime = Date.now();
    const elapsedTime = (currentTime - startTime) / 1000; // Calculate elapsed time in seconds with 2 decimal places
    // Update a text field (you can customize this element by ID)
    document.getElementById('timerDisplay').textContent = `Time: ${elapsedTime.toFixed(2)} seconds`;
}

// Function to toggle the value of recordingOngoing.
function toggleRecording() {
    recordingOngoing = !recordingOngoing; // Toggle the value.
}

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






