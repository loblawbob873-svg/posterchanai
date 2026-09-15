"""Run the shipped DeskView gesture code with a deterministic Android event/timer boundary.

The native device case separately exercises Android dispatch and attachment. This host
check runs the complete Java class (not a copied gesture implementation) before deploy.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / "mobile/android/app/src/main/java/place/poster/app/home"
STUBS = {
"android/content/Context.java": "package android.content; public class Context {}",
"android/graphics/Rect.java": "package android.graphics; public class Rect { int l,t,r,b; public void set(int l,int t,int r,int b){this.l=l;this.t=t;this.r=r;this.b=b;} public boolean contains(int x,int y){return x>=l&&x<r&&y>=t&&y<b;} }",
"android/graphics/Paint.java": "package android.graphics; public class Paint { public static final int ANTI_ALIAS_FLAG=1; public enum Style {STROKE,FILL} public Paint(int f){} public void setStyle(Style s){} public void setStrokeWidth(float f){} public void setColor(int c){} }",
"android/graphics/Canvas.java": "package android.graphics; public class Canvas { public void drawLine(float a,float b,float c,float d,Paint p){} public void drawRoundRect(float a,float b,float c,float d,float e,float f,Paint p){} public void drawCircle(float a,float b,float c,Paint p){} }",
"android/view/MotionEvent.java": "package android.view; public class MotionEvent { public static final int ACTION_DOWN=0,ACTION_UP=1,ACTION_MOVE=2,ACTION_CANCEL=3; int a; float x,y; public MotionEvent(int a,float x,float y){this.a=a;this.x=x;this.y=y;} public int getActionMasked(){return a;} public float getX(){return x;} public float getY(){return y;} }",
"android/view/ViewConfiguration.java": "package android.view; public class ViewConfiguration { public static ViewConfiguration get(android.content.Context c){return new ViewConfiguration();} public int getScaledTouchSlop(){return 8;} public int getScaledMinimumFlingVelocity(){return 50;} }",
"android/view/VelocityTracker.java": "package android.view; public class VelocityTracker { public static VelocityTracker obtain(){return new VelocityTracker();} public void recycle(){} public void addMovement(MotionEvent e){} public void computeCurrentVelocity(int n){} public float getYVelocity(){return 0;} }",
"android/view/HapticFeedbackConstants.java": "package android.view; public class HapticFeedbackConstants { public static final int LONG_PRESS=0; }",
"android/view/View.java": """package android.view; public class View {
 android.content.Context c; int w=400,h=500; public Runnable timer;
 public View(android.content.Context c){this.c=c;} public android.content.Context getContext(){return c;}
 public void setClickable(boolean b){} public void setLongClickable(boolean b){} public void requestLayout(){} public void invalidate(){}
 public int getWidth(){return w;} public int getHeight(){return h;} public int getMeasuredWidth(){return w;} public int getMeasuredHeight(){return h;}
 public void measure(int w,int h){this.w=w;this.h=h;} public void layout(int l,int t,int r,int b){w=r-l;h=b-t;}
 public boolean postDelayed(Runnable r,long ms){timer=r;return true;} public boolean removeCallbacks(Runnable r){if(timer==r)timer=null;return true;}
 public boolean performHapticFeedback(int n){return true;} public boolean onTouchEvent(MotionEvent e){return false;}
 public static class MeasureSpec {public static final int EXACTLY=1; public static int getSize(int s){return s;} public static int makeMeasureSpec(int s,int m){return s;}}
 }""",
"android/view/ViewGroup.java": """package android.view; public class ViewGroup extends View {
 public ViewGroup(android.content.Context c){super(c);} public void setWillNotDraw(boolean b){} public void setClipChildren(boolean b){}
 public void removeAllViews(){} public void addView(View v){} protected void onMeasure(int w,int h){} protected void onLayout(boolean c,int l,int t,int r,int b){}
 public void setMeasuredDimension(int w,int h){} protected void dispatchDraw(android.graphics.Canvas c){}
 public void requestDisallowInterceptTouchEvent(boolean b){} public boolean onInterceptTouchEvent(MotionEvent e){return false;}
 }""",
"place/poster/app/ui/PcTheme.java": "package place.poster.app.ui; public class PcTheme { public static class Palette {public int accent,radiusDp;} }",
"place/poster/app/ui/Skin.java": "package place.poster.app.ui; public class Skin {public static int dp(android.content.Context c,int n){return n;} public static int alpha(int c,double a){return c;} }",
"place/poster/app/home/HomeMetrics.java": "package place.poster.app.home; public class HomeMetrics {public static int swipeUpMinPx(int s,int h){return h/5;} }",
}
DRIVER = r"""
package place.poster.app.home;
import android.view.*;
import java.util.*;
public class TouchDriver {
 static class Host implements DeskView.Host {
  Desk.Item menu; int changes,opens;
  public View viewFor(Desk.Item i){return new View(new android.content.Context());}
  public void onOpen(Desk.Item i){opens++;} public void onLongPress(Desk.Item i){menu=i;}
  public void onLongPressEmpty(){} public void onSwipeUp(){} public void onChanged(){changes++;}
  public int minSpanX(Desk.Item i){return 1;} public int minSpanY(Desk.Item i){return 1;}
  public int maxSpanX(Desk.Item i){return 4;} public int maxSpanY(Desk.Item i){return 5;}
  public boolean resizable(Desk.Item i){return false;} public void onResized(Desk.Item i,int w,int h){}
 }
 static void check(boolean ok,String why){if(!ok)throw new AssertionError(why);}
 static void event(DeskView d,int a,float x,float y){d.onTouchEvent(new MotionEvent(a,x,y));}
 static DeskView desk(Host h,boolean full){
  DeskView d=new DeskView(new android.content.Context());d.bind(h,null);d.setGrid(4,5);
  List<Desk.Item> rows=new ArrayList<>();
  for(int i=0;i<(full?20:1);i++)rows.add(new Desk.Item("app:"+i,i%4,i/4,1,1));
  d.setItems(rows);return d;
 }
 static void hold(DeskView d){event(d,0,50,50);check(d.timer!=null,"long press not armed");event(d,2,51,50);check(d.timer!=null,"pre-hold jitter cancelled timer");d.timer.run();check(d.editingItem()!=null,"not lifted");}
 public static void main(String[] args){
  Host h=new Host();DeskView d=desk(h,true);hold(d);
  event(d,2,51,51);event(d,2,49,50);event(d,1,49,50);
  check(h.menu==d.items().get(0),"full grid: sub-slop jitter swallowed Remove menu");
  check(h.changes==0&&h.opens==0&&d.items().size()==20,"hold changed/launched app");
  h=new Host();d=desk(h,false);hold(d);
  event(d,2,150,50);event(d,1,150,50);
  check(h.menu==null,"intentional drag opened menu");
  check(d.items().get(0).col==1&&h.changes==1,"first MOVE outside icon failed to drag");
  h=new Host();d=desk(h,true);hold(d);event(d,2,150,50);event(d,1,150,50);
  check(h.menu==null&&d.items().get(0).col==0&&d.items().size()==20,"occupied drag damaged full grid");
  h=new Host();d=desk(h,true);hold(d);event(d,2,51,50);event(d,3,51,50);
  check(h.menu==null&&h.opens==0,"cancel opened app/menu");
  System.out.println("touch contracts passed");
 }
}
"""


def test_full_launcher_long_press_jitter_and_deliberate_drag(tmp_path):
    sources = []
    for name, body in {**STUBS, "place/poster/app/home/TouchDriver.java": DRIVER}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        sources.append(str(path))
    subprocess.run(["javac", "-d", str(tmp_path), *sources,
                    str(HOME / "Desk.java"), str(HOME / "DeskView.java")], check=True, capture_output=True, text=True, timeout=30)
    result = subprocess.run(["java", "-cp", str(tmp_path), "place.poster.app.home.TouchDriver"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
