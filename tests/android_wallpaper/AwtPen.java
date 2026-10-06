package place.poster.app.wallpaper;

import java.awt.*;
import java.awt.geom.*;
import java.awt.image.BufferedImage;
import java.io.File;
import java.util.HashMap;
import java.util.Map;
import javax.imageio.ImageIO;

/** The wallpaper's Pen over java.awt — renders the SHIPPED CyberScene to a PNG off-device. Test only. */
public class AwtPen implements Pen {
    final Graphics2D g;
    /** The dance frames, as PNGs in the directory named by -Dpc.frames (dance-1.png .. dance-8.png). */
    static BufferedImage[] frames;
    static final Map<Long, BufferedImage> tinted = new HashMap<>();

    public AwtPen(BufferedImage img) {
        g = img.createGraphics();
        g.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON);
        g.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING, RenderingHints.VALUE_TEXT_ANTIALIAS_ON);
        g.setRenderingHint(RenderingHints.KEY_INTERPOLATION, RenderingHints.VALUE_INTERPOLATION_BILINEAR);
        if (frames == null) {
            frames = new BufferedImage[CyberScene.FRAMES];
            String dir = System.getProperty("pc.frames");
            for (int i = 0; dir != null && i < frames.length; i++) {
                try { frames[i] = ImageIO.read(new File(dir, "dance-" + (i + 1) + ".png")); } catch (Exception ignored) { }
            }
        }
    }
    static Color c(int argb) { return new Color(argb, true); }
    public void fillRect(float x, float y, float w, float h, int argb) { g.setPaint(c(argb)); g.fill(new Rectangle2D.Float(x, y, w, h)); }
    public void fillRectVGrad(float x, float y, float w, float h, int top, int bottom) {
        g.setPaint(new GradientPaint(0, y, c(top), 0, y + Math.max(1, h), c(bottom))); g.fill(new Rectangle2D.Float(x, y, w, h)); }
    public void fillCircleVGrad(float cx, float cy, float r, int top, int bottom) {
        g.setPaint(new GradientPaint(0, cy - r, c(top), 0, cy + r, c(bottom))); g.fill(new Ellipse2D.Float(cx - r, cy - r, 2 * r, 2 * r)); }
    public void radialGlow(float cx, float cy, float r, int inner) {
        g.setPaint(new RadialGradientPaint(cx, cy, Math.max(1, r), new float[]{0f, 1f}, new Color[]{c(inner), c(inner & 0x00FFFFFF)}));
        g.fill(new Ellipse2D.Float(cx - r, cy - r, 2 * r, 2 * r)); }
    public void line(float x1, float y1, float x2, float y2, float width, int argb) {
        g.setPaint(c(argb)); g.setStroke(new BasicStroke(width)); g.draw(new Line2D.Float(x1, y1, x2, y2)); }
    public void strokeRect(float x, float y, float w, float h, float width, int argb, float glow) {
        if (glow > 0) for (int i = 3; i >= 1; i--) { g.setPaint(c((argb & 0x00FFFFFF) | (0x22 << 24))); g.setStroke(new BasicStroke(width + glow * i / 1.5f)); g.draw(new Rectangle2D.Float(x, y, w, h)); }
        g.setPaint(c(argb)); g.setStroke(new BasicStroke(width)); g.draw(new Rectangle2D.Float(x, y, w, h)); }
    public void text(String s, float cx, float baseline, float size, int argb, float glow) {
        g.setFont(new Font(Font.MONOSPACED, Font.BOLD, Math.max(1, Math.round(size))));
        float tw = g.getFontMetrics().stringWidth(s);
        if (glow > 0) { g.setPaint(c(((argb >>> 24) / 5 << 24) | (argb & 0x00FFFFFF)));
            for (int dx = -2; dx <= 2; dx++) for (int dy = -2; dy <= 2; dy++) g.drawString(s, cx - tw / 2 + dx * glow / 3, baseline + dy * glow / 3); }
        g.setPaint(c(argb)); g.drawString(s, cx - tw / 2, baseline); }
    public void ring(float cx, float cy, float rx, float ry, float width, int argb) {
        g.setPaint(c(argb)); g.setStroke(new BasicStroke(width)); g.draw(new Ellipse2D.Float(cx - rx, cy - ry, 2 * rx, 2 * ry)); }
    public void scanlines(float x, float y, float w, float h, int argb, float pitch) {
        g.setPaint(c(argb)); g.setStroke(new BasicStroke(1f));
        for (float yy = y; yy < y + h; yy += pitch) g.draw(new Line2D.Float(x, yy, x + w, yy)); }
    public void sprite(int frame, float x, float y, float w, float h, float alpha, int tint, boolean flipV) {
        if (frames == null || frame < 0 || frame >= frames.length || frames[frame] == null) return;
        BufferedImage src = tint == 0 ? frames[frame] : tint(frame, tint);
        Composite old = g.getComposite();
        g.setComposite(AlphaComposite.getInstance(AlphaComposite.SRC_OVER, Math.max(0f, Math.min(1f, alpha))));
        if (flipV) g.drawImage(src, Math.round(x), Math.round(y + h), Math.round(x + w), Math.round(y), 0, 0, src.getWidth(), src.getHeight(), null);
        else g.drawImage(src, Math.round(x), Math.round(y), Math.round(x + w), Math.round(y + h), 0, 0, src.getWidth(), src.getHeight(), null);
        g.setComposite(old);
    }
    /** Luminance in the tint's hue: the same mapping CanvasPen's colour matrix makes. */
    static BufferedImage tint(int frame, int tint) {
        long key = ((long) frame << 32) | (tint & 0xFFFFFFFFL);
        BufferedImage t = tinted.get(key);
        if (t != null) return t;
        BufferedImage s = frames[frame];
        t = new BufferedImage(s.getWidth(), s.getHeight(), BufferedImage.TYPE_INT_ARGB);
        float tr = ((tint >> 16) & 255) / 255f, tg = ((tint >> 8) & 255) / 255f, tb = (tint & 255) / 255f;
        for (int yy = 0; yy < s.getHeight(); yy++) for (int xx = 0; xx < s.getWidth(); xx++) {
            int p = s.getRGB(xx, yy);
            float lum = (0.3f * ((p >> 16) & 255) + 0.59f * ((p >> 8) & 255) + 0.11f * (p & 255)) / 255f;
            float l = Math.min(1f, 0.25f + lum * 1.2f);
            t.setRGB(xx, yy, (p & 0xFF000000) | ((int) (255 * tr * l) << 16) | ((int) (255 * tg * l) << 8) | (int) (255 * tb * l));
        }
        tinted.put(key, t);
        return t;
    }
}
