package place.poster.app.wallpaper;

import java.awt.image.BufferedImage;
import java.io.File;
import javax.imageio.ImageIO;

/**
 * Renders frames exactly the way CyberWallpaper composes them on a phone: the sky layer, the skyline
 * layer blitted at the parallax shift, then the dynamic frame. args: out.png width height t offset [tapX tapY tapStartSeconds]
 */
public class Render {
    public static void main(String[] a) throws Exception {
        int w = Integer.parseInt(a[1]), h = Integer.parseInt(a[2]);
        float t = Float.parseFloat(a[3]), off = Float.parseFloat(a[4]);
        CyberScene s = new CyberScene(w, h, 0x50C4L);
        BufferedImage img = new BufferedImage(w, h, BufferedImage.TYPE_INT_ARGB);
        AwtPen p = new AwtPen(img);
        s.drawSky(p);
        // the skyline layer, as the engine caches it: a bitmap from skylineTop to the horizon, SPAN wide
        float top = s.skylineTop();
        BufferedImage city = new BufferedImage((int) Math.ceil(CyberScene.SPAN * w), (int) Math.ceil(s.horizon - top) + 2, BufferedImage.TYPE_INT_ARGB);
        s.drawSkyline(new AwtPen(city), 0, -top);
        p.g.drawImage(city, Math.round(-s.parallaxPx(off)), Math.round(top), null);
        float[] taps = a.length >= 8 ? new float[]{Float.parseFloat(a[5]), Float.parseFloat(a[6]), Float.parseFloat(a[7])} : new float[0];
        s.drawFrame(p, t, off, taps, taps.length / 3);
        ImageIO.write(img, "png", new File(a[0]));
    }
}
