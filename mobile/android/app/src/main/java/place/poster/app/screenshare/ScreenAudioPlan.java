package place.poster.app.screenshare;

/**
 * What a screen share sends as SOUND, and how each half of it is muted. Pure Java on purpose (no Android
 * imports) so tests/test_android_screenshare_audio.py can compile and RUN it with plain javac.
 *
 * Why this exists: the screen share used to stream the MICROPHONE as its only audio source. What viewers heard
 * as "the screen's sound" was the phone's speaker picked up by that mic, so the one Mute button silenced both
 * and there was no way to talk over a game while keeping its sound, or the other way round ("the mute button
 * mutes my mic AND the screen audio"). On Android 10+ the phone's own playback can be captured digitally
 * (AudioPlaybackCapture, through the MediaProjection the share already holds), so the share now sends the two
 * as separate inputs mixed by RootEncoder's MixAudioSource, and each has its own volume: 0 = muted.
 *
 * Two independent states — micMuted and screenMuted — and neither ever touches the other. They are applied
 * through the per-source VOLUME rather than MixAudioSource.mute(), because mute() silences the whole mix.
 */
final class ScreenAudioPlan {

  /** MIC = the microphone only (the pre-Android-10 shape). MIX = microphone + the phone's own playback. */
  enum Source { MIC, MIX }

  /** Android 10 (API 29) is where AudioPlaybackCapture — and so MixAudioSource / InternalAudioSource — exists. */
  static final int PLAYBACK_CAPTURE_SDK = 29;

  /**
   * Which audio source a share should use.
   *
   * There is deliberately no "screen audio only" (InternalAudioSource) branch for a refused microphone:
   * AudioPlaybackCapture is itself gated on RECORD_AUDIO — an AudioRecord built with a playback-capture
   * config needs that permission exactly like a microphone does — so without the mic permission there is no
   * screen audio to capture either, and the plugin refuses the share before it gets here. Offering the branch
   * would be a code path that can only ever fail on a device.
   */
  static Source choose(int sdkInt, boolean recordAudioGranted) {
    return (sdkInt >= PLAYBACK_CAPTURE_SDK && recordAudioGranted) ? Source.MIX : Source.MIC;
  }

  private Source source = Source.MIC;
  private boolean micMuted;
  private boolean screenMuted;

  ScreenAudioPlan(boolean micMuted, boolean screenMuted) {
    this.micMuted = micMuted;
    this.screenMuted = screenMuted;
  }

  void useSource(Source s) { source = (s == null) ? Source.MIC : s; }
  Source source() { return source; }

  /** True only when the phone's playback really is one of the inputs — the client shows its button on this. */
  boolean hasScreenAudio() { return source == Source.MIX; }

  void setMicMuted(boolean v) { micMuted = v; }
  /** Recorded even without screen audio, so a later answer never contradicts what the user last asked for. */
  void setScreenMuted(boolean v) { screenMuted = v; }

  boolean micMuted() { return micMuted; }
  boolean screenMuted() { return screenMuted; }

  float micVolume() { return micMuted ? 0f : 1f; }
  /** 0 whenever there is no screen input at all: nothing to hear, so nothing may be claimed to be on. */
  float screenVolume() { return (screenMuted || source != Source.MIX) ? 0f : 1f; }
}
