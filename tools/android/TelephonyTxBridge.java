import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.os.Build;
import android.os.Process;

import java.io.InputStream;
import java.lang.reflect.Method;

/**
 * Dev-only SHUO Phase-5 supplemental bridge.
 *
 * stdin contract: signed little-endian PCM16, 16 kHz, mono.
 * output contract: an already-active cellular call's TYPE_TELEPHONY sink.
 *
 * This helper never dials, answers, hangs up or writes raw audio to a file.
 * It fails closed unless AudioTrack reports the actual routed device as
 * TYPE_TELEPHONY after playback has started.
 */
public final class TelephonyTxBridge {
    private static final int SAMPLE_RATE = 16000;
    private static final int PCM_BYTES_PER_SAMPLE = 2;
    private static final int STREAM_READ_BYTES = 640;
    private static final int ROUTE_PREROLL_BYTES = 3200;

    private static void log(String line) {
        System.err.println(line);
        System.err.flush();
    }

    private static AudioDeviceInfo findTelephonyDevice() throws Exception {
        Method method = AudioManager.class.getDeclaredMethod(
                "getDevicesStatic", int.class);
        method.setAccessible(true);

        AudioDeviceInfo[] devices = (AudioDeviceInfo[]) method.invoke(
                null, AudioManager.GET_DEVICES_OUTPUTS);

        AudioDeviceInfo match = null;
        for (AudioDeviceInfo device : devices) {
            if (device.getType() != AudioDeviceInfo.TYPE_TELEPHONY) {
                continue;
            }
            if (match != null) {
                throw new IllegalStateException(
                        "multiple TYPE_TELEPHONY output devices are visible");
            }
            match = device;
        }
        return match;
    }

    private static AudioTrack createTrack() {
        int minBuffer = AudioTrack.getMinBufferSize(
                SAMPLE_RATE,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        if (minBuffer <= 0) {
            throw new IllegalStateException(
                    "invalid AudioTrack minimum buffer: " + minBuffer);
        }

        AudioAttributes attributes = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build();

        AudioFormat format = new AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(SAMPLE_RATE)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .build();

        AudioTrack track = new AudioTrack(
                attributes,
                format,
                Math.max(minBuffer, 6400),
                AudioTrack.MODE_STREAM,
                AudioManager.AUDIO_SESSION_ID_GENERATE);
        if (track.getState() != AudioTrack.STATE_INITIALIZED) {
            throw new IllegalStateException(
                    "AudioTrack did not initialize: state=" + track.getState());
        }
        return track;
    }

    private static int writeFully(
            AudioTrack track, byte[] data, int offset, int length) {
        int writtenTotal = 0;
        while (writtenTotal < length) {
            int written = track.write(
                    data, offset + writtenTotal, length - writtenTotal);
            if (written <= 0) {
                throw new IllegalStateException(
                        "AudioTrack.write failed: " + written);
            }
            writtenTotal += written;
        }
        return writtenTotal;
    }

    private static long streamStdin(AudioTrack track) throws Exception {
        InputStream input = System.in;
        byte[] inputBuffer = new byte[STREAM_READ_BYTES];
        byte[] aligned = new byte[STREAM_READ_BYTES + PCM_BYTES_PER_SAMPLE];

        int pendingByte = -1;
        long total = 0;

        while (true) {
            int read = input.read(inputBuffer);
            if (read < 0) {
                break;
            }
            if (read == 0) {
                continue;
            }

            int sourceOffset = 0;
            int alignedLength = 0;

            if (pendingByte >= 0) {
                aligned[alignedLength++] = (byte) pendingByte;
                aligned[alignedLength++] = inputBuffer[sourceOffset++];
                pendingByte = -1;
            }

            int remaining = read - sourceOffset;
            int evenBytes = remaining - (remaining % PCM_BYTES_PER_SAMPLE);
            if (evenBytes > 0) {
                System.arraycopy(
                        inputBuffer,
                        sourceOffset,
                        aligned,
                        alignedLength,
                        evenBytes);
                alignedLength += evenBytes;
                sourceOffset += evenBytes;
            }

            if (sourceOffset < read) {
                pendingByte = inputBuffer[sourceOffset] & 0xff;
            }

            if (alignedLength > 0) {
                total += writeFully(track, aligned, 0, alignedLength);
            }
        }

        if (pendingByte >= 0) {
            throw new IllegalStateException(
                    "stdin ended with an incomplete PCM16 sample");
        }
        return total;
    }

    public static void main(String[] args) {
        try {
            log("SHUO_TELEPHONY_TX_BRIDGE");
            log("SDK=" + Build.VERSION.SDK_INT);
            log("UID=" + Process.myUid());

            AudioDeviceInfo telephony = findTelephonyDevice();
            if (telephony == null) {
                throw new IllegalStateException(
                        "no TYPE_TELEPHONY output device is visible");
            }

            log("TELEPHONY_ID=" + telephony.getId());
            log("TELEPHONY_TYPE=" + telephony.getType());

            AudioTrack track = createTrack();
            if (!track.setPreferredDevice(telephony)) {
                throw new IllegalStateException(
                        "AudioTrack rejected TYPE_TELEPHONY as preferred device");
            }

            track.play();
            byte[] silence = new byte[ROUTE_PREROLL_BYTES];
            writeFully(track, silence, 0, silence.length);
            Thread.sleep(150);

            AudioDeviceInfo routed = track.getRoutedDevice();
            if (routed == null) {
                throw new IllegalStateException(
                        "AudioTrack has no routed device after playback start");
            }

            log("ROUTED_ID=" + routed.getId());
            log("ROUTED_TYPE=" + routed.getType());
            if (routed.getType() != AudioDeviceInfo.TYPE_TELEPHONY) {
                throw new IllegalStateException(
                        "actual AudioTrack route is not TYPE_TELEPHONY");
            }

            log("STREAM_READY");
            long total = streamStdin(track);

            Thread.sleep(300);
            log("PCM_BYTES=" + total);
            log("STREAM_DONE");

            /*
             * On the reference itel runtime, AudioTrack.stop()/release() could
             * remain blocked after Telephony Tx EOF even after all PCM was
             * consumed. This dedicated one-shot app_process therefore uses
             * process exit as its bounded ownership boundary.
             */
            System.exit(0);
        } catch (Throwable t) {
            log("FAIL=" + t.getClass().getName() + ":" + String.valueOf(t.getMessage()));
            t.printStackTrace(System.err);
            System.err.flush();
            System.exit(1);
        }
    }
}
