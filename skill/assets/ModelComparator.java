import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonTypeName;
import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.DecimalMax;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import org.openapitools.jackson.nullable.JsonNullable;

import java.io.File;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.lang.reflect.ParameterizedType;
import java.lang.reflect.Type;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.*;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Compares two packages of OpenAPI-generated model classes by reflection and writes a JSON diff to stdout.
 *
 * <pre>
 *   java -cp &lt;classes:deps&gt; ModelComparator.java &lt;classesDir&gt; &lt;package1&gt; &lt;package2&gt;
 * </pre>
 *
 * Pairing:
 *   1. classes by simple name;
 *   2. leftover classes by structure (Jaccard similarity of "jsonName:type" >= 0.8), which catches schema renames;
 *   3. inside a pair, properties by JSON name, then leftovers by identical (type, required, constraints),
 *      which catches property renames.
 *
 * Output keys use "left" for package1 and "right" for package2. Impact rating is done downstream by
 * render_report.py. This tool only reports facts.
 *
 * Exit codes: 0 ok, 2 usage error, 3 failure while loading classes.
 */
public final class ModelComparator {

    static final double RENAME_THRESHOLD = 0.8;

    record Prop(String json, String javaType, boolean required, boolean nullable, boolean readOnly,
                Map<String, String> constraints, String defaultValue, String description, String example) {
        String signature() { return javaType + "|" + required + "|" + new TreeMap<>(constraints); }
    }

    record Model(String name, String kind, String superclass, String jsonTypeName,
                 LinkedHashMap<String, Prop> props, List<String> enumValues) {}

    public static void main(String[] args) {
        if (args.length != 3) {
            System.err.println("usage: ModelComparator <classesDir> <package1> <package2>");
            System.exit(2);
        }
        try {
            Map<String, Model> left = load(Paths.get(args[0]), args[1]);
            Map<String, Model> right = load(Paths.get(args[0]), args[2]);
            System.out.print(compare(left, right));
        } catch (Exception e) {
            System.err.println("ModelComparator failed: " + e);
            System.exit(3);
        }
    }

    // ------------------------------------------------------------------ pairing

    static String compare(Map<String, Model> left, Map<String, Model> right) {
        List<String[]> pairs = new ArrayList<>();
        Set<String> onlyL = new TreeSet<>(left.keySet()), onlyR = new TreeSet<>(right.keySet());
        for (String n : left.keySet()) {
            if (right.containsKey(n)) { pairs.add(new String[]{n, n, "name", "1.0"}); onlyL.remove(n); onlyR.remove(n); }
        }
        for (String nl : new ArrayList<>(onlyL)) {
            String best = null; double bestScore = 0;
            for (String nr : onlyR) {
                double s = similarity(left.get(nl), right.get(nr));
                if (s > bestScore) { bestScore = s; best = nr; }
            }
            if (best != null && bestScore >= RENAME_THRESHOLD) {
                pairs.add(new String[]{nl, best, "structure", String.format(Locale.ROOT, "%.2f", bestScore)});
                onlyL.remove(nl); onlyR.remove(best);
            }
        }
        pairs.sort(Comparator.comparing(p -> p[0]));

        List<String> pairJson = new ArrayList<>();
        for (String[] p : pairs) pairJson.add(comparePair(left.get(p[0]), right.get(p[1]), p[2], p[3]));

        return "{\n"
                + "  \"onlyLeft\": " + arr(onlyL.stream().map(n -> summary(left.get(n), left)).toList()) + ",\n"
                + "  \"onlyRight\": " + arr(onlyR.stream().map(n -> summary(right.get(n), right)).toList()) + ",\n"
                + "  \"pairs\": [\n" + String.join(",\n", pairJson) + "\n  ]\n}\n";
    }

    static double similarity(Model x, Model y) {
        if (!x.kind().equals(y.kind())) return 0;
        Set<String> a, b;
        if (x.kind().equals("enum")) { a = new HashSet<>(x.enumValues()); b = new HashSet<>(y.enumValues()); }
        else {
            a = x.props().values().stream().map(p -> p.json() + ":" + p.javaType()).collect(Collectors.toSet());
            b = y.props().values().stream().map(p -> p.json() + ":" + p.javaType()).collect(Collectors.toSet());
        }
        Set<String> union = new HashSet<>(a); union.addAll(b);
        Set<String> inter = new HashSet<>(a); inter.retainAll(b);
        return union.isEmpty() ? 0 : (double) inter.size() / union.size();
    }

    static String comparePair(Model x, Model y, String matchedBy, String score) {
        List<String> diffs = new ArrayList<>();
        List<String> equal = new ArrayList<>();
        if (!x.name().equals(y.name())) diffs.add(diff(null, "class.renamed", x.name(), y.name()));
        if (!Objects.equals(x.superclass(), y.superclass())) diffs.add(diff(null, "class.superclass", x.superclass(), y.superclass()));

        if (x.kind().equals("enum")) {
            for (String v : x.enumValues()) if (!y.enumValues().contains(v)) diffs.add(diff(null, "enum.onlyLeft", v, null));
            for (String v : y.enumValues()) if (!x.enumValues().contains(v)) diffs.add(diff(null, "enum.onlyRight", null, v));
        } else {
            Set<String> ol = new LinkedHashSet<>(x.props().keySet()), or = new LinkedHashSet<>(y.props().keySet());
            Map<String, String> matched = new LinkedHashMap<>();
            for (String n : x.props().keySet()) if (y.props().containsKey(n)) { matched.put(n, n); ol.remove(n); or.remove(n); }
            for (String nl : new ArrayList<>(ol)) {
                for (String nr : new ArrayList<>(or)) {
                    if (x.props().get(nl).signature().equals(y.props().get(nr).signature())) {
                        matched.put(nl, nr); ol.remove(nl); or.remove(nr);
                        diffs.add(diff(nl, "property.renamed", nl, nr));
                        break;
                    }
                }
            }
            for (var e : matched.entrySet()) {
                Prop a = x.props().get(e.getKey()), b = y.props().get(e.getValue());
                int before = diffs.size();
                String f = e.getKey();
                cmp(diffs, f, "property.type", a.javaType(), b.javaType());
                cmp(diffs, f, "property.required", "" + a.required(), "" + b.required());
                cmp(diffs, f, "property.nullable", "" + a.nullable(), "" + b.nullable());
                cmp(diffs, f, "property.readOnly", "" + a.readOnly(), "" + b.readOnly());
                Set<String> keys = new TreeSet<>(a.constraints().keySet()); keys.addAll(b.constraints().keySet());
                for (String k : keys) cmp(diffs, f, "constraint." + k, a.constraints().get(k), b.constraints().get(k));
                cmp(diffs, f, "property.default", a.defaultValue(), b.defaultValue());
                cmp(diffs, f, "doc.description", a.description(), b.description());
                cmp(diffs, f, "doc.example", a.example(), b.example());
                if (diffs.size() == before && e.getKey().equals(e.getValue())) equal.add(e.getKey());
            }
            for (String n : ol) { Prop p = x.props().get(n); diffs.add(diff(n, "property.onlyLeft", p.javaType() + (p.required() ? " (required)" : ""), null)); }
            for (String n : or) { Prop p = y.props().get(n); diffs.add(diff(n, "property.onlyRight", null, p.javaType() + (p.required() ? " (required)" : ""))); }
        }
        return "    {\"left\": " + q(x.name()) + ", \"right\": " + q(y.name()) + ", \"kind\": " + q(x.kind())
                + ", \"matchedBy\": " + q(matchedBy) + ", \"score\": " + score
                + ", \"propsLeft\": " + x.props().size() + ", \"propsRight\": " + y.props().size()
                + ", \"enumLeft\": " + strs(x.enumValues()) + ", \"enumRight\": " + strs(y.enumValues())
                + ", \"equalProps\": " + strs(equal) + ", \"diffs\": " + arr(diffs) + "}";
    }

    static void cmp(List<String> diffs, String field, String aspect, String l, String r) {
        if (!Objects.equals(l, r)) diffs.add(diff(field, aspect, l, r));
    }

    // ------------------------------------------------------------------ loading

    static Map<String, Model> load(Path classesDir, String pkg) throws Exception {
        Path dir = classesDir.resolve(pkg.replace('.', File.separatorChar));
        if (!Files.isDirectory(dir)) throw new IllegalArgumentException("no classes for package " + pkg + " at " + dir);
        Map<String, Model> models = new TreeMap<>();
        try (Stream<Path> s = Files.list(dir)) {
            for (Path f : s.filter(p -> p.toString().endsWith(".class") && !p.getFileName().toString().contains("$")).toList()) {
                String simple = f.getFileName().toString().replace(".class", "");
                models.put(simple, describe(Class.forName(pkg + "." + simple)));
            }
        }
        return models;
    }

    static Model describe(Class<?> c) throws Exception {
        JsonTypeName jtn = c.getAnnotation(JsonTypeName.class);
        Class<?> sc = c.getSuperclass();
        String sup = sc == null || sc == Object.class || sc == Enum.class ? null : sc.getSimpleName();
        if (c.isEnum()) {
            List<String> vals = new ArrayList<>();
            Method gv = c.getMethod("getValue");
            for (Object e : c.getEnumConstants()) vals.add(String.valueOf(gv.invoke(e)));
            return new Model(c.getSimpleName(), "enum", null, null, new LinkedHashMap<>(), vals);
        }
        Object instance = null;
        try { instance = c.getDeclaredConstructor().newInstance(); } catch (ReflectiveOperationException ignored) { /* no defaults then */ }

        LinkedHashMap<String, Prop> props = new LinkedHashMap<>();
        for (Class<?> k = c; k != null && k != Object.class; k = k.getSuperclass()) {
            for (Field f : k.getDeclaredFields()) {
                if (Modifier.isStatic(f.getModifiers())) continue;
                Method g = getter(k, f);
                if (g == null || g.getAnnotation(JsonProperty.class) == null) continue;
                String json = g.getAnnotation(JsonProperty.class).value();
                Schema schema = g.getAnnotation(Schema.class);

                Type t = f.getGenericType();
                boolean nullable = f.getType() == JsonNullable.class;
                if (nullable) t = ((ParameterizedType) t).getActualTypeArguments()[0];

                Map<String, String> cons = new TreeMap<>();
                Size size = g.getAnnotation(Size.class);
                if (size != null) {
                    if (size.min() != 0) cons.put("minLength", "" + size.min());
                    if (size.max() != Integer.MAX_VALUE) cons.put("maxLength", "" + size.max());
                }
                Min min = g.getAnnotation(Min.class);               if (min != null) cons.put("minimum", "" + min.value());
                Max max = g.getAnnotation(Max.class);               if (max != null) cons.put("maximum", "" + max.value());
                DecimalMin dmin = g.getAnnotation(DecimalMin.class); if (dmin != null) cons.put("minimum", dmin.value());
                DecimalMax dmax = g.getAnnotation(DecimalMax.class); if (dmax != null) cons.put("maximum", dmax.value());
                Pattern pat = g.getAnnotation(Pattern.class);       if (pat != null) cons.put("pattern", pat.regexp());
                if (g.getAnnotation(Email.class) != null) cons.put("format", "email");

                String def = null;
                if (instance != null) {
                    f.setAccessible(true);
                    Object v = f.get(instance);
                    if (v instanceof JsonNullable<?> jn) v = jn.isPresent() ? jn.get() : null;
                    if (v != null && !(v instanceof Collection<?> col && col.isEmpty())
                            && !(v instanceof Map<?, ?> m && m.isEmpty())) def = String.valueOf(v);
                }
                boolean required = g.getAnnotation(NotNull.class) != null
                        || (schema != null && schema.requiredMode() == Schema.RequiredMode.REQUIRED);
                props.put(json, new Prop(json, typeName(t), required, nullable,
                        schema != null && schema.accessMode() == Schema.AccessMode.READ_ONLY, cons, def,
                        schema == null || schema.description().isEmpty() ? null : schema.description(),
                        schema == null || schema.example().isEmpty() ? null : schema.example()));
            }
        }
        return new Model(c.getSimpleName(), "class", sup, jtn == null ? null : jtn.value(), props, List.of());
    }

    static Method getter(Class<?> k, Field f) {
        String n = Character.toUpperCase(f.getName().charAt(0)) + f.getName().substring(1);
        for (String pre : new String[]{"get", "is"}) {
            try { return k.getDeclaredMethod(pre + n); } catch (NoSuchMethodException ignored) { /* try next */ }
        }
        return null;
    }

    /** Simple type names, so spec1.Address and spec2.Address compare equal. */
    static String typeName(Type t) {
        if (t instanceof Class<?> c) return c.getSimpleName();
        if (t instanceof ParameterizedType p) {
            return ((Class<?>) p.getRawType()).getSimpleName() + "<"
                    + Arrays.stream(p.getActualTypeArguments()).map(ModelComparator::typeName).collect(Collectors.joining(", ")) + ">";
        }
        return t.getTypeName();
    }

    // ------------------------------------------------------------------ JSON

    static String summary(Model m, Map<String, Model> all) {
        List<String> refs = new ArrayList<>();
        for (Model o : all.values()) {
            for (Prop p : o.props().values()) {
                if (p.javaType().equals(m.name()) || p.javaType().contains("<" + m.name() + ">")) refs.add(o.name() + "." + p.json());
            }
        }
        return "{\"name\": " + q(m.name()) + ", \"kind\": " + q(m.kind()) + ", \"jsonTypeName\": " + q(m.jsonTypeName())
                + ", \"referencedBy\": " + strs(refs)
                + ", \"props\": " + strs(m.props().values().stream().map(p -> p.json() + ": " + p.javaType() + (p.required() ? " (required)" : "")).toList())
                + ", \"enum\": " + strs(m.enumValues()) + "}";
    }

    static String diff(String field, String aspect, String l, String r) {
        return "{\"field\": " + q(field) + ", \"aspect\": " + q(aspect) + ", \"left\": " + q(l) + ", \"right\": " + q(r) + "}";
    }

    static String strs(List<String> items) { return arr(items.stream().map(ModelComparator::q).toList()); }

    static String arr(List<String> items) { return "[" + String.join(", ", items) + "]"; }

    static String q(String s) {
        if (s == null) return "null";
        StringBuilder b = new StringBuilder("\"");
        for (char ch : s.toCharArray()) {
            switch (ch) {
                case '"' -> b.append("\\\"");
                case '\\' -> b.append("\\\\");
                case '\n' -> b.append("\\n");
                case '\r' -> b.append("\\r");
                case '\t' -> b.append("\\t");
                default -> { if (ch < 0x20) b.append(String.format("\\u%04x", (int) ch)); else b.append(ch); }
            }
        }
        return b.append('"').toString();
    }
}
