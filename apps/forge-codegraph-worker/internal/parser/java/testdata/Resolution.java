package demo;
import outside.Client;
import java.util.*;
import static outside.Tools.save;
import static outside.Constants.*;

/** Dependency extraction fixture; external names intentionally need binding. */
@Route(path = {"/a", "/b"}, enabled = false, nested = @Meta(value = String.class))
class Demo<T extends Comparable<T> & java.io.Serializable> extends Base<T> implements Runnable {
    java.util.List<? extends T> values;
    Client service;
    int first[], second[][];
    static { initialize(); }
    { setup(); }
    Demo() { this(1); }
    Demo(int n) { super(n); }
    public void run() {
        service.save(1);
        { Other service = null; service.save(2); }
        service.save(3);
        service.next().save(4);
        ((Client) service).save(5);
        clients[0].save(6);
        java.util.function.Consumer<String> fn = this::accept;
        java.util.function.Supplier<Client> make = Client::new;
        java.util.function.IntFunction<Client[]> arrayFactory = Client[]::new;
        java.util.function.Supplier<java.util.List<String>> listFactory = java.util.ArrayList<String>::new;
        java.util.function.Function<String, String> lambda = item -> transform(item);
        java.util.function.Consumer<String> block = (String item) -> { accept(item); };
        new Client(7).save(8);
        Runnable anonymous = new Runnable() { public void run() { service.save(9); } };
        class Local { void local() { accept("local"); } }
        int[] numbers = new int[count()][2];
        numbers[0] += 1;
        service.value++;
        values = null;
        for (int i = 0; i < 2; i++) { accept("loop"); }
        for (String item : items()) { accept(item); }
        try (Client resource = open()) { resource.save(10); }
        catch (FirstException | SecondException failure) { handle(failure); }
        finally { cleanup(); }
    }
    void accept(String value) {}
    <R> R convert(R value) throws FirstException { return value; }
    int result()[] { return new int[] {1, 2}; }
    void qualified() { Demo.super.run(); Demo.super.field.save(); outer.new Inner(); }
    class Inner { void receiver(Demo.Inner this) {} }
}

enum Choice {
    ONE(1) { void apply() { work(); } }, TWO(2);
    Choice(int value) {}
}

record Entry<T>(T key, String... tags) implements Comparable<Entry<T>> {
    Entry { check(key); }
    public int compareTo(Entry<T> other) { return 0; }
}

@interface Meta { Class<?> value() default Object.class; }
