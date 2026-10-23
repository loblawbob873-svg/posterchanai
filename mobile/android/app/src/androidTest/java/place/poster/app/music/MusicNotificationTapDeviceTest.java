package place.poster.app.music;

import android.content.Context;
import android.view.KeyEvent;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import place.poster.app.RealUi;

/**
 * THE MUSIC NOTIFICATION'S BUTTONS, PRESSED IN THE SHADE with the app in the background.
 *
 * MusicCarButtonsDeviceTest reaches the session through key events and a MediaController it builds
 * itself. A person uses neither: they pull the shade down and press the media controls SystemUI drew
 * from the notification and its session. Those controls are the one surface every phone shows, the
 * one the app does not draw, and the one where "the button does nothing" was first reported. Here the
 * shade is really opened, the button found by what TalkBack calls it, pressed with a real touch, and
 * the result read off the page's own audio element.
 */
@RunWith(AndroidJUnit4.class)
public class MusicNotificationTapDeviceTest {

    private PlayingPage page;

    @Before
    public void setUp() {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            RealUi.shell("pm grant " + ctx.getPackageName() + " android.permission.POST_NOTIFICATIONS");
        }
        RealUi.awake();
    }

    @After
    public void tearDown() {
        RealUi.shell("cmd statusbar collapse");
        if (page != null) page.close();
    }

    @Test
    public void pausePlayNextAndPreviousFromTheShadeReachThePage() throws Exception {
        page = PlayingPage.start();
        InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_HOME);

        press("Pause", RealUi.named("Pause"));
        page.expect("PAUSE in the notification shade", "window.__pcBackgroundAudio.paused===true");

        press("Play", RealUi.named("Play"));
        page.expect("PLAY in the notification shade", "window.__pcBackgroundAudio.paused===false");

        press("Next", RealUi.saying("next"));
        page.expect("NEXT in the notification shade", "(window.__pcNext||0)>=1");

        press("Previous", RealUi.saying("previous"));
        page.expect("PREVIOUS in the notification shade", "(window.__pcPrev||0)>=1");
    }

    /** Pull the shade down (again: a press can collapse it) and press the control. */
    private void press(String what, RealUi.Match m) {
        if (RealUi.find(m) == null) RealUi.shell("cmd statusbar expand-notifications");
        RealUi.tap("the music notification's " + what + " button in the shade", m, 10_000);
    }
}
