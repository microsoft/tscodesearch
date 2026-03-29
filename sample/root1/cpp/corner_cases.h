/**
 * corner_cases.h — synthetic C++ corner-case fixture.
 *
 * Each section tests a specific syntactic form that could trip up the
 * tree-sitter AST walker.  All names are fictional.
 *
 * Sections:
 *   A. Trailing-return-type functions
 *   B. Operator overloads
 *   C. Deleted / defaulted special members
 *   D. Nested classes (with inheritance)
 *   E. Multi-level namespace  (A::B::C style)
 *   F. Anonymous namespace
 *   G. Template class with out-of-line member definitions
 *   H. Virtual + multiple inheritance
 *   I. Constexpr / inline free functions
 *   J. Lambda call sites inside methods
 */

#pragma once
#include <cstdint>
#include <functional>

// ─── A. Trailing-return-type functions ────────────────────────────────────────

auto compute_sum(int a, int b) -> int;
auto make_label(const char* prefix) -> const char*;

struct Adder {
    auto add(int x, int y) const -> int { return x + y; }
    auto scale(float f) -> float;   // prototype only
};


// ─── B. Operator overloads ────────────────────────────────────────────────────

struct Vec2 {
    float x, y;

    Vec2  operator+(const Vec2& other) const { return {x + other.x, y + other.y}; }
    Vec2& operator+=(const Vec2& other)      { x += other.x; y += other.y; return *this; }
    bool  operator==(const Vec2& other) const { return x == other.x && y == other.y; }
    float operator[](int idx) const           { return idx == 0 ? x : y; }
    Vec2  operator-() const                   { return {-x, -y}; }

    // Non-member friend (declared inside class for convenience)
    friend Vec2 operator*(float s, const Vec2& v) { return {s * v.x, s * v.y}; }
};


// ─── C. Deleted / defaulted special members ───────────────────────────────────

struct NonCopyable {
    NonCopyable()                            = default;
    ~NonCopyable()                           = default;
    NonCopyable(const NonCopyable&)          = delete;
    NonCopyable& operator=(const NonCopyable&) = delete;
    NonCopyable(NonCopyable&&)               = default;
    NonCopyable& operator=(NonCopyable&&)    = default;
};


// ─── D. Nested classes (with inheritance) ─────────────────────────────────────

class IWidget {
public:
    virtual void draw()  = 0;
    virtual void clear() = 0;
    virtual ~IWidget()   = default;
};

class Panel : public IWidget {
public:
    // Nested class inheriting from an outer interface
    class Label : public IWidget {
    public:
        void draw()  override {}
        void clear() override {}
        void set_text(const char* t) { (void)t; }
    };

    // Nested struct (plain data)
    struct Bounds {
        int x, y, w, h;
    };

    void draw()  override {}
    void clear() override {}
    Label* make_label() { return new Label(); }
    Bounds bounds() const { return {0, 0, 100, 100}; }
};


// ─── E. Multi-level namespace  (A::B style) ───────────────────────────────────

namespace HAL {
namespace GPIO {

class Pin {
public:
    enum class Mode { Input, Output, Alternate };
    void     set_mode(Mode m);
    bool     read() const;
    void     write(bool v);
    uint32_t port_id() const;
};

class Port {
public:
    Pin* get_pin(uint8_t idx);
    void reset_all();
};

} // namespace GPIO
} // namespace HAL


// ─── F. Anonymous namespace ───────────────────────────────────────────────────

namespace {

struct InternalHelper {
    static int clamp(int v, int lo, int hi);
    static float lerp(float a, float b, float t);
};

void internal_reset(int* buf, int len);

} // anonymous namespace


// ─── G. Template class ────────────────────────────────────────────────────────

template<typename T>
class RingBuffer {
public:
    explicit RingBuffer(int capacity);
    void push(const T& item);
    T    pop();
    bool empty() const;
    int  size()  const;

private:
    T*  _data;
    int _head, _tail, _cap;
};

// Out-of-line template definitions (in the same TU — legal in headers)
template<typename T>
RingBuffer<T>::RingBuffer(int capacity) : _data(nullptr), _head(0), _tail(0), _cap(capacity) {}

template<typename T>
void RingBuffer<T>::push(const T& item) {
    _data[_tail % _cap] = item;
    ++_tail;
}

template<typename T>
T RingBuffer<T>::pop() {
    return _data[_head++ % _cap];
}


// ─── H. Multiple + virtual inheritance ───────────────────────────────────────

class ISerializable {
public:
    virtual void serialize(uint8_t* buf, int len) = 0;
    virtual int  serialized_size() const          = 0;
    virtual ~ISerializable()                      = default;
};

class ILoggable {
public:
    virtual void log_state() const = 0;
    virtual ~ILoggable()           = default;
};

// Multiple inheritance
class Sensor : public ISerializable, public ILoggable {
public:
    void serialize(uint8_t* buf, int len) override;
    int  serialized_size() const          override;
    void log_state()       const          override;
    void update(float dt);
};

// Virtual base
class VehicleBase {
public:
    virtual void arm()   = 0;
    virtual void disarm()= 0;
    virtual ~VehicleBase()= default;
};

class Plane : public virtual VehicleBase {
public:
    void arm()    override {}
    void disarm() override {}
    void roll(float deg);
};

class Copter : public virtual VehicleBase {
public:
    void arm()    override {}
    void disarm() override {}
    void yaw(float deg);
};

// Diamond: both parents share VehicleBase
class VTOL : public Plane, public Copter {
public:
    void arm()    override {}
    void disarm() override {}
    void transition();
};


// ─── I. Constexpr / inline free functions ─────────────────────────────────────

constexpr int   kMaxChannels = 16;
constexpr float kDegToRad(float deg) { return deg * 3.14159265f / 180.0f; }

inline uint32_t pack_u16(uint16_t hi, uint16_t lo) {
    return (static_cast<uint32_t>(hi) << 16) | lo;
}


// ─── J. Lambda call sites ─────────────────────────────────────────────────────

class Dispatcher {
public:
    using Callback = std::function<void(int)>;

    void register_cb(Callback cb);

    void fire_all(int val) {
        // calls register_cb and a lambda that itself calls helper()
        register_cb([this](int v) {
            helper(v * 2);
        });
        auto fn = [](int x) { return x + 1; };
        (void)fn(val);
    }

    void helper(int x);
};
