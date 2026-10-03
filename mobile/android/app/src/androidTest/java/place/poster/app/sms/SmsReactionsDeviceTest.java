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
    /** "that reaction thing that Android and iphone do when long-pressing on the message": the bar. */
    @Test public void longPressBarSixReactionsYoursHighlightedTapToTakeBackAndMore() {
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            Context c=context();SmsMsg original=row(1,"Received text",1);
            List<String> sent=new ArrayList<>();int[] more={0},done={0};
            LinearLayout bar=SmsReactionViews.barView(c,history(original),original,PcTheme.of("dark"),
                    (kind,remove) -> sent.add(SmsReactions.format(kind,remove,original.body)),
                    () -> more[0]++, () -> done[0]++);
            assertNotNull(bar);
            assertEquals(7,bar.getChildCount());
            for(int i=0;i<6;i++) assertFalse(bar.getChildAt(i).isSelected());
            bar.getChildAt(0).performClick();bar.getChildAt(0).performClick();
            assertEquals(1,sent.size());assertEquals(1,done[0]);
            SmsReactions.Parsed p=SmsReactions.parse(sent.get(0));
            assertEquals("heart",p.kind);assertEquals("add",p.operation);
            // Already liked: the like is highlighted and tapping it takes it back.
            SmsMsg liked=row(2,"Liked “Received text”",2);
            sent.clear();
            LinearLayout again=SmsReactionViews.barView(c,history(original,liked),original,PcTheme.of("dark"),
                    (kind,remove) -> sent.add(SmsReactions.format(kind,remove,original.body)), () -> more[0]++, null);
            assertTrue(again.getChildAt(1).isSelected());
            again.getChildAt(1).performClick();
            assertEquals("remove",SmsReactions.parse(sent.get(0)).operation);
            assertEquals("like",SmsReactions.parse(sent.get(0)).kind);
            // ⋯ is the rest of the message menu.
            LinearLayout third=SmsReactionViews.barView(c,history(original),original,PcTheme.of("dark"),
                    (kind,remove) -> fail(), () -> more[0]++, null);
            third.getChildAt(6).performClick();
            assertEquals(1,more[0]);
            // Nothing to react to (a reaction row itself): no bar, the menu opens instead.
            assertNull(SmsReactionViews.barView(c,history(original,liked),liked,PcTheme.of("dark"),(k,r)->fail(),null,null));
        });
    }
}
