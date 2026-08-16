package place.poster.app.sync;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * A small JSON reader/writer for the native sweep. Android-free, so `tests/test_android_native_sync.py`
 * can compile and RUN it here rather than only on CI.
 *
 * WHY NOT org.json, WHICH ANDROID ALREADY SHIPS. Two reasons, and the first has already cost this
 * codebase a whole feature: org.json's writer escapes a forward slash as {@code \/}. That is legal
 * JSON and it changes the BYTES, and this app has a bug on record where exactly that produced a
 * different sha256 for an event and every quote post silently failed to publish. Here the same trap
 * is worse: the manifest is serialised, encrypted and hashed, and a document written by the phone has
 * to be byte-comparable with one written by the browser. `JSON.stringify` is the format on the other
 * end of this wire, so the writer here matches `JSON.stringify` and nothing else.
 *
 * The second reason is testability: the org.json in `tests/androidstubs` is signature-only (it has to
 * be — it stands in for a platform class), so anything that PARSES with it cannot be executed off a
 * device. The engine that decides whether files get deleted must be runnable in a test.
 *
 * Numbers follow JS: an integral value is written without a decimal point, so a size or an mtime
 * round-trips as the same text it arrived as.
 */
public final class Json {

    private Json() { }

    // ------------------------------------------------------------------------------- reading

    public static Object parse(String src) {
        P p = new P(src);
        p.ws();
        Object v = p.value();
        p.ws();
        if (p.i < p.s.length()) throw new IllegalArgumentException("trailing junk at " + p.i);
        return v;
    }

    /** The object at `v`, or an empty one — never null, because every caller here wants a map. */
    @SuppressWarnings("unchecked")
    public static Map<String, Object> obj(Object v) {
        return v instanceof Map ? (Map<String, Object>) v : new LinkedHashMap<String, Object>();
    }

    @SuppressWarnings("unchecked")
    public static List<Object> arr(Object v) {
        return v instanceof List ? (List<Object>) v : new ArrayList<Object>();
    }

    public static String str(Object v, String dflt) {
        return v instanceof String ? (String) v : dflt;
    }

    public static long num(Object v, long dflt) {
        if (v instanceof Long) return (Long) v;
        if (v instanceof Double) return (long) (double) (Double) v;
        return dflt;
    }

    public static boolean bool(Object v, boolean dflt) {
        return v instanceof Boolean ? (Boolean) v : dflt;
    }

    private static final class P {
        final String s;
        int i;

        P(String s) { this.s = s; }

        void ws() {
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') i++; else break;
            }
        }

        Object value() {
            if (i >= s.length()) throw new IllegalArgumentException("unexpected end");
            char c = s.charAt(i);
            switch (c) {
                case '{': return object();
                case '[': return array();
                case '"': return string();
                case 't': expect("true"); return Boolean.TRUE;
                case 'f': expect("false"); return Boolean.FALSE;
                case 'n': expect("null"); return null;
                default: return number();
            }
        }

        void expect(String word) {
            if (!s.startsWith(word, i)) throw new IllegalArgumentException("bad literal at " + i);
            i += word.length();
        }

        Map<String, Object> object() {
            Map<String, Object> out = new LinkedHashMap<String, Object>();
            i++;                                        // {
            ws();
            if (i < s.length() && s.charAt(i) == '}') { i++; return out; }
            while (true) {
                ws();
                String k = string();
                ws();
                if (i >= s.length() || s.charAt(i) != ':') throw new IllegalArgumentException("expected : at " + i);
                i++;
                ws();
                out.put(k, value());
                ws();
                if (i >= s.length()) throw new IllegalArgumentException("unterminated object");
                char c = s.charAt(i++);
                if (c == '}') return out;
                if (c != ',') throw new IllegalArgumentException("expected , or } at " + (i - 1));
            }
        }

        List<Object> array() {
            List<Object> out = new ArrayList<Object>();
            i++;                                        // [
            ws();
            if (i < s.length() && s.charAt(i) == ']') { i++; return out; }
            while (true) {
                ws();
                out.add(value());
                ws();
                if (i >= s.length()) throw new IllegalArgumentException("unterminated array");
                char c = s.charAt(i++);
                if (c == ']') return out;
                if (c != ',') throw new IllegalArgumentException("expected , or ] at " + (i - 1));
            }
        }

        String string() {
            if (i >= s.length() || s.charAt(i) != '"') throw new IllegalArgumentException("expected string at " + i);
            i++;
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (i >= s.length()) throw new IllegalArgumentException("unterminated string");
                char c = s.charAt(i++);
                if (c == '"') return sb.toString();
                if (c != '\\') { sb.append(c); continue; }
                if (i >= s.length()) throw new IllegalArgumentException("unterminated escape");
                char e = s.charAt(i++);
                switch (e) {
                    case '"':  sb.append('"');  break;
                    case '\\': sb.append('\\'); break;
                    case '/':  sb.append('/');  break;
                    case 'b':  sb.append('\b'); break;
                    case 'f':  sb.append('\f'); break;
                    case 'n':  sb.append('\n'); break;
                    case 'r':  sb.append('\r'); break;
                    case 't':  sb.append('\t'); break;
                    case 'u':
                        if (i + 4 > s.length()) throw new IllegalArgumentException("short \\u");
                        sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                        break;
                    default: throw new IllegalArgumentException("bad escape \\" + e);
                }
            }
        }

        Object number() {
            int start = i;
            if (i < s.length() && (s.charAt(i) == '-' || s.charAt(i) == '+')) i++;
            boolean real = false;
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c >= '0' && c <= '9') { i++; continue; }
                if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') { real = true; i++; continue; }
                break;
            }
            String t = s.substring(start, i);
            if (t.isEmpty()) throw new IllegalArgumentException("expected a value at " + start);
            if (!real) {
                try { return Long.valueOf(t); } catch (NumberFormatException ignored) { }
            }
            return Double.valueOf(t);
        }
    }

    // ------------------------------------------------------------------------------- writing

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        put(sb, v);
        return sb.toString();
    }

    private static void put(StringBuilder sb, Object v) {
        if (v == null) { sb.append("null"); return; }
        if (v instanceof String) { string(sb, (String) v); return; }
        if (v instanceof Boolean) { sb.append(((Boolean) v) ? "true" : "false"); return; }
        if (v instanceof Number) { number(sb, (Number) v); return; }
        if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (e.getValue() == UNDEFINED) continue;      // JSON.stringify drops undefined members
                if (!first) sb.append(',');
                first = false;
                string(sb, String.valueOf(e.getKey()));
                sb.append(':');
                put(sb, e.getValue());
            }
            sb.append('}');
            return;
        }
        if (v instanceof Iterable) {
            sb.append('[');
            boolean first = true;
            for (Object o : (Iterable<?>) v) {
                if (!first) sb.append(',');
                first = false;
                put(sb, o == UNDEFINED ? null : o);           // …and writes them as null inside an array
            }
            sb.append(']');
            return;
        }
        string(sb, String.valueOf(v));
    }

    /** A member that must not be written at all, the way an absent JS property is not written. */
    public static final Object UNDEFINED = new Object();

    private static void number(StringBuilder sb, Number n) {
        if (n instanceof Long || n instanceof Integer || n instanceof Short || n instanceof Byte) {
            sb.append(n.longValue());
            return;
        }
        double d = n.doubleValue();
        if (Double.isNaN(d) || Double.isInfinite(d)) { sb.append("null"); return; }   // as JSON.stringify does
        if (d == Math.rint(d) && Math.abs(d) < 9.007199254740992E15) {
            sb.append((long) d);                                                      // 3.0 -> "3", as JS writes it
            return;
        }
        sb.append(d);
    }

    /* Matches JSON.stringify: quote, backslash and the C0 controls, and NOTHING ELSE. In particular a
     * forward slash is written as itself — see the class comment for what escaping it once cost. */
    private static void string(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); continue;
                case '\\': sb.append("\\\\"); continue;
                case '\b': sb.append("\\b");  continue;
                case '\f': sb.append("\\f");  continue;
                case '\n': sb.append("\\n");  continue;
                case '\r': sb.append("\\r");  continue;
                case '\t': sb.append("\\t");  continue;
                default: break;
            }
            if (c < 0x20) {
                sb.append(String.format("\\u%04x", (int) c));
                continue;
            }
            /* A lone surrogate is not valid UTF-8 and cannot be encoded — and a filename CAN contain
             * one (an unpaired half survives a copy from a broken archive). JS's well-formed
             * stringify escapes it rather than emitting invalid text; so does this, which keeps the
             * document parseable instead of failing the whole sweep on one bad name. */
            if (Character.isHighSurrogate(c)) {
                if (i + 1 < s.length() && Character.isLowSurrogate(s.charAt(i + 1))) {
                    sb.append(c).append(s.charAt(++i));
                } else {
                    sb.append(String.format("\\u%04x", (int) c));
                }
                continue;
            }
            if (Character.isLowSurrogate(c)) {
                sb.append(String.format("\\u%04x", (int) c));
                continue;
            }
            sb.append(c);
        }
        sb.append('"');
    }
}
