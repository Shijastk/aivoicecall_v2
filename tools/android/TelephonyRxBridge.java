import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Process;

import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;

/**
 * Dev-only SHUO Phase-5 supplemental caller-side receive bridge.
 *
 * Capture contract:
 *   Android VOICE_DOWNLINK -> PCM16 little-endian / 48 kHz / stereo -> stdout
 *
 * stderr contains only content-free lifecycle/format diagnostics.
 * The helper never writes an audio file and never performs call control.
 *
 * This source intentionally uses only API-23 compile-time types. Runtime
 * validation is Android 13 on the reference itel P683L.
 */
public final class TelephonyRxBridge {
    private static final int SAMPLE_RATE = 48000;
    private static final int CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_STEREO;
    private static final int CHANNELS = 2;
    private static final int ENCODING = AudioFormat.ENCODING_PCM_16BIT;
    private static final int MAX_READ_BYTES = 4096;

    private static void log(String line) {
        System.err.println(line);
        System.err.flush();
    }

    private static AudioRecord createRecorder() {
        int minBuffer = AudioRecord.getMinBufferSize(
                SAMPLE_RATE,
                CHANNEL_CONFIG,
                ENCODING);

        if (minBuffer <= 0) {
            throw new IllegalStateException(
                    "invalid AudioRecord minimum buffer: " + minBuffer);
        }

        AudioFormat format = new AudioFormat.Builder()
                .setEncoding(ENCODING)
                .setSampleRate(SAMPLE_RATE)
                .setChannelMask(CHANNEL_CONFIG)
                .build();

        AudioRecord recorder = new AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.VOICE_DOWNLINK)
                .setAudioFormat(format)
                .setBufferSizeInBytes(8 * minBuffer)
                .build();

        if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
            throw new IllegalStateException(
                    "AudioRecord did not initialize: state=" + recorder.getState());
        }

        if (recorder.getSampleRate() != SAMPLE_RATE) {
            throw new IllegalStateException(
                    "unexpected sample rate: " + recorder.getSampleRate());
        }

        if (recorder.getChannelCount() != CHANNELS) {
            throw new IllegalStateException(
                    "unexpected channel count: " + recorder.getChannelCount());
        }

        if (recorder.getAudioFormat() != ENCODING) {
            throw new IllegalStateException(
                    "unexpected encoding: " + recorder.getAudioFormat());
        }

        return recorder;
    }

    public static void main(String[] args) {
        try {
            log("SHUO_TELEPHONY_RX_BRIDGE");
            log("SDK=" + Build.VERSION.SDK_INT);
            log("UID=" + Process.myUid());
            log("SOURCE=VOICE_DOWNLINK");

            if (Build.VERSION.SDK_INT < 23) {
                throw new IllegalStateException("Android API 23+ is required");
            }

            AudioRecord recorder = createRecorder();
            recorder.startRecording();

            if (recorder.getRecordingState()
                    != AudioRecord.RECORDSTATE_RECORDING) {
                throw new IllegalStateException(
                        "AudioRecord did not enter RECORDSTATE_RECORDING");
            }

            log("PCM_RATE=" + recorder.getSampleRate());
            log("PCM_CHANNELS=" + recorder.getChannelCount());
            log("PCM_ENCODING=PCM_16BIT");
            log("STREAM_READY");

            byte[] buffer = new byte[MAX_READ_BYTES];
            FileOutputStream output = new FileOutputStream(FileDescriptor.out);
            long total = 0;
            int writesSinceFlush = 0;

            while (true) {
                int read = recorder.read(buffer, 0, buffer.length);

                if (read < 0) {
                    throw new IllegalStateException(
                            "AudioRecord.read failed: " + read);
                }

                if (read == 0) {
                    continue;
                }

                if ((read % (CHANNELS * 2)) != 0) {
                    throw new IllegalStateException(
                            "AudioRecord returned a partial stereo PCM16 frame: "
                            + read);
                }

                try {
                    output.write(buffer, 0, read);
                } catch (IOException brokenPipe) {
                    log("PCM_BYTES=" + total);
                    log("STREAM_DONE");
                    System.exit(0);
                    return;
                }

                total += read;
                writesSinceFlush++;

                if (writesSinceFlush >= 4) {
                    try {
                        output.flush();
                    } catch (IOException brokenPipe) {
                        log("PCM_BYTES=" + total);
                        log("STREAM_DONE");
                        System.exit(0);
                        return;
                    }
                    writesSinceFlush = 0;
                }
            }
        } catch (Throwable t) {
            log("FAIL="
                    + t.getClass().getName()
                    + ":"
                    + String.valueOf(t.getMessage()));
            t.printStackTrace(System.err);
            System.err.flush();
            System.exit(1);
        }
    }
}
