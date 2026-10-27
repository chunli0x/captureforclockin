# Reviewed shipped consumer rules: see docs/ANDROID-HARDENING.md.
# AGP merges each AAR's proguard.txt and embedded JAR rules automatically.
# Do not keep entire library or application packages.

# Mobile Wallet Adapter 2.2.0 (clientlib, clientlib-ktx, common): consumer rules
# are empty. Its JSON-RPC uses explicit JSONObject access and direct callbacks;
# no reflective DTO/callback keep rule is necessary.

# ML Kit common 18.11.0 keeps @UsedBy* native/reflection entry points; face
# detection 16.1.7 keeps native names and its generated protobuf fields.
# LiteRT 2.1.5 keeps @UsedByReflection. Preserve names of surviving JNI members
# only; unused wrappers/methods may still be removed (also covered by AGP's defaults).
-keepclasseswithmembernames,includedescriptorclasses class org.tensorflow.lite.** {
    native <methods>;
}

# CameraX 1.6.2 consumer rules keep Camera2Config$DefaultProvider, Quirk
# implementations and the platform onReadoutStarted callback. Its lifecycle,
# video, view and camera2-pipe artifacts explicitly support shrinking. No additions.

# Coil 3.6.2 core keeps GenericViewTarget subclasses. The network artifact has
# a META-INF/services entry but no consumer rules. Retain only its provider
# constructor for service discovery, not all Coil or OkHttp classes.
-keep,allowoptimization class coil3.network.okhttp.internal.OkHttpNetworkFetcherServiceLoaderTarget {
    public <init>();
}

# Serialization entry points: app/MWA use org.json; Sentry uses explicit
# JsonSerializable/JsonDeserializer implementations. No field-name reflection
# over app data classes, Gson or kotlinx.serialization is in use.
# ML Kit's protobuf reflection is already covered by its own consumer rules.

# Sentry Android core 8.60.0 ships rules for options constructors, integration
# names, enums and Throwable identity. Keep source locations for offline retrace;
# archive mapping.txt for every distributed release. Do not keep all SDK classes.
-keepattributes SourceFile,LineNumberTable
