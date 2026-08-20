package place.poster.app.sms;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.net.Uri;

import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * `sms:` URIs, parsed against the REAL android.net.Uri.
 *
 * SendTo is pure Java and tests/test_android_sms.py could run it under a stub — which is exactly why
 * it is here instead. The whole difficulty of this parser is Uri's own behaviour: an opaque URI
 * answers null to getQueryParameter, `sms://n` puts the number in the authority while `sms:n` puts
 * it in the scheme-specific part, and Uri.decode has its own idea of what a `+` is. A stub that got
 * any of that wrong would agree with the code and prove nothing.
 *
 * These shapes are all observed in the wild; getting one wrong means a "Text" link in a web page
 * opens an empty compose screen, which reads as the app being broken by whoever tapped it.
 */
@RunWith(AndroidJUnit4.class)
public class SendToDeviceTest {

    @Test
    public void theOrdinaryShapes() {
        assertEquals("+15550100", SendTo.numberFrom(Uri.parse("sms:+15550100")));
        assertEquals("5550100", SendTo.numberFrom(Uri.parse("smsto:5550100")));
        assertEquals("+15550100", SendTo.numberFrom(Uri.parse("sms://+15550100")));
        assertEquals("+15550100", SendTo.numberFrom(Uri.parse("mmsto:+15550100")));
    }

    @Test
    public void percentEncoded() {
        assertEquals("+15550100", SendTo.numberFrom(Uri.parse("smsto:%2B15550100")));
    }

    @Test
    public void severalRecipients() {
        assertEquals(2, SendTo.numbersFrom(Uri.parse("sms:+1555,+1666")).size());
        assertEquals("+1555", SendTo.numberFrom(Uri.parse("sms:+1555,+1666")));
    }

    @Test
    public void rfc5724Body() {
        assertEquals("hi there", SendTo.bodyFrom(Uri.parse("sms:+15550100?body=hi%20there")));
        assertEquals("+15550100", SendTo.numberFrom(Uri.parse("sms:+15550100?body=hi%20there")));
        // The non-opaque form goes through Uri's own query parser instead.
        assertEquals("hi", SendTo.bodyFrom(Uri.parse("sms://+15550100?body=hi")));
    }

    @Test
    public void nothingUsefulIsNotAnError() {
        assertEquals("", SendTo.numberFrom(null));
        assertEquals("", SendTo.numberFrom(Uri.parse("sms:")));
        assertEquals("", SendTo.bodyFrom(Uri.parse("sms:+15550100")));
    }

    @Test
    public void onlyTheFourSchemesCount() {
        assertTrue(SendTo.isMessageUri(Uri.parse("SMSTO:+15550100")));
        assertTrue(!SendTo.isMessageUri(Uri.parse("tel:+15550100")));
        assertTrue(!SendTo.isMessageUri(null));
    }
}
