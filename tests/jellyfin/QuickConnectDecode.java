import java.nio.file.Files;
import java.nio.file.Path;
import kotlinx.serialization.json.Json;
import org.jellyfin.sdk.model.api.QuickConnectResult;

/** Decode a captured adapter response with the official Kotlin JVM SDK, including required fields. */
public final class QuickConnectDecode {
    public static void main(String[] args) throws Exception {
        QuickConnectResult result = Json.Default.decodeFromString(
                QuickConnectResult.Companion.serializer(), Files.readString(Path.of(args[0])));
        if (result.getSecret().isEmpty() || !result.getCode().matches("[0-9]{6}"))
            throw new AssertionError("Missing pairing identity");
        System.out.println("PASS official Kotlin SDK decoded QuickConnectResult");
    }
}
