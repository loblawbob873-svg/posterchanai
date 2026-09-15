"""Execute the shipped native clipboard/IME boundary, including permission ownership."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_clipboard_and_keyboard_content_are_consumed_once_without_early_permission_release(tmp_path):
    files={
'android/R.java':'package android;public class R{public static class id{public static int paste=1;}}',
'android/net/Uri.java':'package android.net;public class Uri{final String s;public Uri(String s){this.s=s;}public String getScheme(){return s.split(":")[0];}}',
'android/content/Context.java':'package android.content;public class Context{public static String CLIPBOARD_SERVICE="clip";public ClipboardManager clipboard=new ClipboardManager();public Object getSystemService(String s){return clipboard;}}',
'android/content/ClipboardManager.java':'package android.content;public class ClipboardManager{public ClipData clip;public boolean denied;public ClipData getPrimaryClip(){if(denied)throw new SecurityException();return clip;}}',
'android/content/ClipDescription.java':'package android.content;public class ClipDescription{public boolean image;public ClipDescription(boolean b){image=b;}public boolean hasMimeType(String s){return image;}}',
'android/content/ClipData.java':'package android.content;import android.net.Uri;public class ClipData{public int count=1;public Uri uri;public ClipDescription desc;public ClipData(boolean image,Uri u){desc=new ClipDescription(image);uri=u;}public ClipDescription getDescription(){return desc;}public int getItemCount(){return count;}public Item getItemAt(int i){return new Item();}public class Item{public Uri getUri(){return uri;}}}',
'android/os/Build.java':'package android.os;public class Build{public static class VERSION{public static int SDK_INT=35;}}',
'android/os/Bundle.java':'package android.os;public class Bundle{}',
'android/util/AttributeSet.java':'package android.util;public interface AttributeSet{}',
'android/view/inputmethod/EditorInfo.java':'package android.view.inputmethod;public class EditorInfo{public String[] contentMimeTypes;}',
'android/view/inputmethod/InputConnection.java':'package android.view.inputmethod;public interface InputConnection{int INPUT_CONTENT_GRANT_READ_URI_PERMISSION=1;default boolean commitContent(InputContentInfo c,int f,android.os.Bundle b){return false;}}',
'android/view/inputmethod/InputConnectionWrapper.java':'package android.view.inputmethod;public class InputConnectionWrapper implements InputConnection{public InputConnectionWrapper(InputConnection c,boolean b){}}',
'android/view/inputmethod/InputContentInfo.java':'''package android.view.inputmethod;import android.net.Uri;import android.content.ClipDescription;public class InputContentInfo{
public int requested,released;public boolean deny,dead;public Uri uri=new Uri("content://image");public ClipDescription getDescription(){return new ClipDescription(true);}public Uri getContentUri(){return uri;}
public void requestPermission(){requested++;if(deny)throw new SecurityException();}public void releasePermission(){released++;if(dead)throw new SecurityException();}}''',
'android/widget/EditText.java':'package android.widget;import android.content.Context;import android.util.AttributeSet;import android.view.inputmethod.*;public class EditText{final Context ctx;public EditText(Context c,AttributeSet a){ctx=c;}public Context getContext(){return ctx;}public boolean onTextContextMenuItem(int id){return false;}public InputConnection onCreateInputConnection(EditorInfo i){return new InputConnection(){};}}',
'place/poster/app/sms/Probe.java':'''package place.poster.app.sms;
import android.content.*;import android.net.Uri;import android.view.inputmethod.*;
public class Probe{
static int images,errors;static Runnable release;static boolean fail;
static void check(boolean b,String s){if(!b)throw new AssertionError(s);}
public static void main(String[] args){Context ctx=new Context();SmsComposeInput input=new SmsComposeInput(ctx,null);
input.setImageReceiver(new SmsComposeInput.Receiver(){public void image(Uri u,Runnable r){images++;release=r;if(fail)throw new SecurityException();}public void error(String m){errors++;}});
ctx.clipboard.clip=new ClipData(false,null);check(!input.onTextContextMenuItem(1),"text paste must use platform");
ctx.clipboard.clip=new ClipData(true,new Uri("content://photo"));check(input.onTextContextMenuItem(1)&&images==1,"clipboard image not delivered");release.run();
ctx.clipboard.clip.count=2;input.onTextContextMenuItem(1);check(images==1&&errors==1,"multiple images silently truncated");
ctx.clipboard.denied=true;input.onTextContextMenuItem(1);check(errors==2,"clipboard denial not reported");
EditorInfo editor=new EditorInfo();InputConnection connection=input.onCreateInputConnection(editor);check(editor.contentMimeTypes[0].equals("image/*"),"IME image support missing");
InputContentInfo content=new InputContentInfo();check(connection.commitContent(content,1,null),"keyboard image not accepted");
check(content.requested==1&&content.released==0,"grant released before copy");release.run();release.run();check(content.released==1,"grant not released exactly once");
content=new InputContentInfo();content.deny=true;check(!connection.commitContent(content,1,null)&&errors==3,"permission denial ignored");
content=new InputContentInfo();content.dead=true;fail=true;connection.commitContent(content,1,null);check(content.released==1&&errors==4,"dead binder escaped cleanup");fail=false;
content=new InputContentInfo();content.uri=new Uri("file:///secret");connection.commitContent(content,1,null);check(content.released==1&&errors==5,"unsupported scheme accepted");
System.out.println("COMPLETE");}}
'''}
    for name,body in files.items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body)
    source=ROOT/'mobile/android/app/src/main/java/place/poster/app/sms/SmsComposeInput.java'
    built=subprocess.run(['javac','-d',str(tmp_path),str(source),*[str(tmp_path/name) for name in files]],capture_output=True,text=True,timeout=20)
    assert built.returncode==0,built.stderr
    ran=subprocess.run(['java','-cp',str(tmp_path),'place.poster.app.sms.Probe'],capture_output=True,text=True,timeout=10)
    assert ran.returncode==0,ran.stderr
    assert 'COMPLETE' in ran.stdout
