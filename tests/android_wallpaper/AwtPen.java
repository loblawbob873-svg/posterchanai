package place.poster.app.wallpaper;

import java.awt.*;
import java.awt.geom.*;
import java.awt.image.BufferedImage;

/** The wallpaper's Pen over java.awt — renders the SHIPPED CyberScene to a PNG off-device. Test only. */
public class AwtPen implements Pen {
    final Graphics2D g;
    public AwtPen(BufferedImage img) {
        g = img.createGraphics();
        g.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON);
        g.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING, RenderingHints.VALUE_TEXT_ANTIALIAS_ON);
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
}
