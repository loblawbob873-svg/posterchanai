package place.poster.app.sms;

import static org.junit.Assert.*;
import android.app.AlertDialog;
import android.content.Context;
import android.view.ContextThemeWrapper;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ListView;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.util.*;
import place.poster.app.R;
import place.poster.app.ui.PcTheme;

/** Real framework layout, recycled chips and picker clicks; no carrier or provider mutations. */
@RunWith(AndroidJUnit4.class)
public class SmsReactionsDeviceTest {
    private static SmsMsg row(long id, String text, int type) {
        SmsMsg m = new SmsMsg();m.id=id;m.threadId=9;m.address="+15551234567";
        m.date=id*1000;m.type=type;m.body=text;return m;
    }
    private static SmsReactionThread history(SmsMsg... rows) {
        return new SmsReactionThread(Arrays.asList(rows),true,9,"+15551234567");
    }
    private Context context() {
        return new ContextThemeWrapper(InstrumentationRegistry.getInstrumentation()
                .getTargetContext(), android.R.style.Theme_Material_Light);
    }
    @Test public void chipsRetainOriginalDetailsAndRecycledRowsLoseOldReactions() {
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            Context c=context();
            LinearLayout host=LayoutInflater.from(c).inflate(R.layout.sms_bubble,null)
                    .findViewById(R.id.pc_b_reactions);
            SmsMsg original=row(1,"My message",2), reaction=row(2,"Loved “My message”",1);
            SmsReactionThread h=history(original,reaction);
            String[] details={null};
            SmsReactionViews.bind(host,h,original,PcTheme.of("dark"),text -> details[0]=text);
            assertEquals(View.VISIBLE,host.getVisibility());
            assertEquals(1,host.getChildCount());
            assertTrue(((Button)host.getChildAt(0)).getText().length()>0);
            host.getChildAt(0).performClick();
            assertEquals(reaction.body,details[0]);
            assertEquals(2,h.raw.size());
            SmsReactionViews.bind(host,history(original),original,PcTheme.of("dark"),text -> fail());
            assertEquals(View.GONE,host.getVisibility());
            assertEquals(0,host.getChildCount());
        });
    }
    @Test public void sixChoicesRemoveAndDoubleTapPreserveComposer() {
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            Context c=context();SmsMsg original=row(1,"Received text",1);
            EditText composer=new EditText(c);composer.setText("Keep my unsent draft");
            composer.setSelection(5);
            String[] kinds={"heart","like","dislike","laugh","emphasize","question"};
            for(int i=0;i<6;i++) {
                List<String> sent=new ArrayList<>();
                AlertDialog picker=SmsReactionViews.picker(c,history(original),original,
                        (kind,remove) -> sent.add(SmsReactions.format(kind,remove,original.body)));
                assertNotNull(picker);picker.create();ListView choices=picker.getListView();
                assertEquals(6,choices.getAdapter().getCount());
                choices.performItemClick(null,i,i);choices.performItemClick(null,i,i);
                assertEquals(1,sent.size());
                assertEquals(kinds[i],SmsReactions.parse(sent.get(0)).kind);
                assertEquals("Keep my unsent draft",composer.getText().toString());
                assertEquals(5,composer.getSelectionStart());picker.dismiss();
            }
            SmsMsg added=row(2,"Liked “Received text”",2);
            List<String> sent=new ArrayList<>();
            AlertDialog picker=SmsReactionViews.picker(c,history(original,added),original,
                    (kind,remove) -> sent.add(SmsReactions.format(kind,remove,original.body)));
            picker.create();assertEquals(7,picker.getListView().getAdapter().getCount());
            picker.getListView().performItemClick(null,6,6);
            assertEquals("remove",SmsReactions.parse(sent.get(0)).operation);picker.dismiss();
            added.type=4;
            assertNull(SmsReactionViews.picker(c,history(original,added),original,(k,r)->fail()));
            assertNull(SmsReactionViews.picker(c,history(original,row(3,original.body,1)),original,(k,r)->fail()));
        });
    }
}
