from pathlib import Path

ROOT=Path(__file__).parents[1]
W=(ROOT/'mobile/android/app/src/main/java/place/poster/app/music/MusicWidget.java').read_text()
S=(ROOT/'mobile/android/app/src/main/java/place/poster/app/music/MusicService.java').read_text()
X=(ROOT/'mobile/android/app/src/main/res/layout/widget_music.xml').read_text()


def test_widget_adapts_to_actual_host_width():
    assert 'OPTION_APPWIDGET_MIN_WIDTH' in W
    assert 'boolean compact = width < 240, tiny = width < 170' in W
    assert 'R.id.mw_prev, active && !compact' in W
    assert 'R.id.mw_art, tiny ? View.GONE' in W
    assert 'onAppWidgetOptionsChanged' in W
    assert 'android:id="@+id/mw_art"' in X


def test_car_metadata_is_one_clean_bounded_line():
    assert 'static String oneLine' in W
    assert 'METADATA_KEY_TITLE, MusicWidget.oneLine(title, 120)' in S
    assert 'METADATA_KEY_ARTIST, MusicWidget.oneLine(artist, 90)' in S
    assert '.setContentTitle(MusicWidget.oneLine(title, 90))' in S

