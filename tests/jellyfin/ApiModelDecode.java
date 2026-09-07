import java.nio.charset.StandardCharsets;
import kotlinx.serialization.KSerializer;
import kotlinx.serialization.json.Json;
import kotlinx.serialization.json.JsonKt;

/** Run the official SDK serializer, using the same tolerant settings as its ApiSerializer. */
public final class ApiModelDecode {
    public static void main(String[] args) throws Exception {
        Json json = JsonKt.Json(Json.Default, builder -> {
            builder.setIgnoreUnknownKeys(true);
            builder.setExplicitNulls(false);
            builder.setCoerceInputValues(true);
            return kotlin.Unit.INSTANCE;
        });
        Class<?> model = Class.forName("org.jellyfin.sdk.model.api." + args[0]);
        Object companion = model.getField("Companion").get(null);
        KSerializer<?> serializer = (KSerializer<?>) companion.getClass().getMethod("serializer").invoke(companion);
        json.decodeFromString(serializer, new String(System.in.readAllBytes(), StandardCharsets.UTF_8));
        System.out.println("PASS " + args[0]);
    }
}
