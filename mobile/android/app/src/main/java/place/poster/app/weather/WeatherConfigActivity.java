package place.poster.app.weather;

import android.app.Activity;
import android.appwidget.AppWidgetManager;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

import place.poster.app.R;
import place.poster.app.ui.PcTheme;
import place.poster.app.ui.PcThemeStore;
import place.poster.app.ui.Skin;

/**
 * "WHERE ARE YOU" — the widget's own configuration activity, started by the system the moment the
 * widget is placed and again whenever somebody taps a widget that has no place yet.
 *
 * A TYPED PLACE, NOT A SENSED ONE. There is no location permission anywhere in this feature: the
 * name is looked up through this node's `/api/weather/geocode`, which is the same endpoint the
 * desktop's picker uses. A permission prompt for a home-screen widget is a bad bargain, and a place
 * somebody chose beats a fix from a cold GPS.
 *
 * NATIVE, like every other screen the launcher can reach. It inflates no WebView, so it works with
 * the browser engine dead — which matters more here than usual, because a widget's configuration
 * activity that will not start leaves the classic grey box and no way to fix it.
 *
 * THE RESULT IS SET BEFORE ANYTHING ELSE CAN GO WRONG. A configuration activity that finishes
 * without RESULT_OK makes the system DELETE the widget it was configuring, so cancelling is set up
 * front and only replaced once a place has actually been stored.
 */
public class WeatherConfigActivity extends Activity {

    private int widgetId = AppWidgetManager.INVALID_APPWIDGET_ID;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<double[]> coords = new ArrayList<double[]>();
    private final List<String> names = new ArrayList<String>();
    private ArrayAdapter<String> adapter;
    private TextView note;
    private Runnable pending;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        Intent in = getIntent();
        if (in != null && in.getExtras() != null) {
            widgetId = in.getExtras().getInt(AppWidgetManager.EXTRA_APPWIDGET_ID,
                    AppWidgetManager.INVALID_APPWIDGET_ID);
        }
        // Cancelled unless and until a place is chosen — see the class comment.
        setResult(RESULT_CANCELED, new Intent()
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId));

        PcTheme.Palette pal = PcThemeStore.palette(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackground(Skin.page(pal, 1.0));
        int p = Skin.dp(this, 16);
        root.setPadding(p, p, p, p);

        TextView title = new TextView(this);
        title.setText(R.string.weather_pick_title);
        title.setTextColor(pal.text);
        title.setTextSize(19);
        root.addView(title);

        note = new TextView(this);
        note.setTextColor(pal.muted);
        note.setTextSize(12);
        note.setPadding(0, Skin.dp(this, 6), 0, Skin.dp(this, 10));
        note.setText(WeatherStore.hasServer(this)
                ? getString(R.string.weather_pick_hint)
                : getString(R.string.weather_need_server));
        root.addView(note);

        final EditText q = new EditText(this);
        q.setHint(R.string.weather_pick_hint_field);
        q.setSingleLine(true);
        q.setTextColor(pal.text);
        q.setHintTextColor(pal.muted);
        q.setBackground(Skin.panel(this, pal));
        int q4 = Skin.dp(this, 11);
        q.setPadding(q4, q4, q4, q4);
        // API 26, and minSdk is 23 — calling it unguarded is a NoSuchMethodError on exactly the
        // old phones nobody here can test on. A town name is not a credential, but offering to save
        // it is noise on a one-field screen.
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
            q.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        }
        root.addView(q);

        ListView list = new ListView(this);
        adapter = new ArrayAdapter<String>(this, android.R.layout.simple_list_item_1, names) {
            @Override public View getView(int i, View reuse, ViewGroup parent) {
                View v = super.getView(i, reuse, parent);
                if (v instanceof TextView) {
                    ((TextView) v).setTextColor(PcThemeStore.palette(WeatherConfigActivity.this).text);
                }
                return v;
            }
        };
        list.setAdapter(adapter);
        list.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        list.setOnItemClickListener(new AdapterView.OnItemClickListener() {
            @Override public void onItemClick(AdapterView<?> a, View v, int i, long id) { choose(i); }
        });
        root.addView(list);

        // TYPING IS DEBOUNCED, because the search is a network call per keystroke otherwise — the
        // same reason the client's picker debounces, and the node caches geocoding for a day.
        q.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void onTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void afterTextChanged(Editable e) {
                final String term = e.toString();
                if (pending != null) main.removeCallbacks(pending);
                pending = new Runnable() { @Override public void run() { search(term); } };
                main.postDelayed(pending, 350);
            }
        });

        setContentView(root, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setTitle(R.string.weather_pick_title);
        root.setGravity(Gravity.TOP);
    }

    private void search(final String term) {
        if (!WeatherStore.hasServer(this)) return;
        new Thread(new Runnable() {
            @Override public void run() {
                final JSONArray r = WeatherFetch.search(WeatherConfigActivity.this, term);
                main.post(new Runnable() { @Override public void run() { show(r); } });
            }
        }, "pcai-weather-geo").start();
    }

    private void show(JSONArray r) {
        names.clear();
        coords.clear();
        for (int i = 0; i < r.length(); i++) {
            JSONObject o = r.optJSONObject(i);
            if (o == null || o.isNull("lat") || o.isNull("lon")) continue;
            StringBuilder b = new StringBuilder(o.optString("name", ""));
            String admin = o.optString("admin", "");
            String country = o.optString("country", "");
            if (!admin.isEmpty()) b.append(", ").append(admin);
            if (!country.isEmpty()) b.append(", ").append(country);
            names.add(b.toString());
            coords.add(new double[]{ o.optDouble("lat"), o.optDouble("lon") });
        }
        adapter.notifyDataSetChanged();
        // AN EMPTY ANSWER SAYS WHICH KIND OF EMPTY IT IS. "no matches" and "I could not ask" send
        // somebody looking in two completely different places.
        if (names.isEmpty()) {
            note.setText(WeatherStore.hasServer(this)
                    ? getString(R.string.weather_pick_none)
                    : getString(R.string.weather_need_server));
        }
    }

    private void choose(int i) {
        if (i < 0 || i >= coords.size()) return;
        double[] c = coords.get(i);
        WeatherStore.setPlace(this, c[0], c[1], names.get(i));
        final Activity self = this;
        new Thread(new Runnable() {
            @Override public void run() {
                WeatherFetch.refresh(self.getApplicationContext());
                WeatherWidget.paint(self.getApplicationContext());
            }
        }, "pcai-weather-first").start();
        setResult(RESULT_OK, new Intent()
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId));
        finish();
    }
}
