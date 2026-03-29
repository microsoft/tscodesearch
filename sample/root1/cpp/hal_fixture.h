/**
 * hal_fixture.h — C++ fixture for tree-sitter AST tests.
 *
 * Covers four previously-buggy scenarios:
 *   1. Qualified base-class matching (NS::Base) without leaking template args
 *   2. Qualified class-name declarations (class NS::Foo)
 *   3. Qualified call sites (NS::func())
 *   4. Pure-virtual / member-function declarations in class bodies
 */

#pragma once
#include <stdint.h>
#include <string>

// ── Namespace + simple base class ─────────────────────────────────────────────

namespace HAL {

class AnalogSource {
public:
    virtual float  read()          = 0;
    virtual void   set_pin(int p)  = 0;
    virtual       ~AnalogSource()  = default;
};

// Bug 4: pure-virtual member function declarations
class AnalogIn {
public:
    virtual AnalogSource* channel(int n)   = 0;
    virtual void          init()           = 0;
    virtual              ~AnalogIn()       = default;
};

// Bug 1: template base class — Scheduler<T> must NOT surface T as a base type
template<typename T>
class Scheduler {
public:
    virtual void register_timer(T callback, uint32_t period_us) = 0;
    virtual void delay(uint32_t ms) = 0;
};

} // namespace HAL


// ── Derived classes (bugs 1 and 2) ────────────────────────────────────────────

// Bug 1: qualified base class — only "AnalogIn" should appear, not "HAL"
class ChibiOSAnalogIn : public HAL::AnalogIn {
public:
    HAL::AnalogSource* channel(int n) override;
    void               init() override;

private:
    int _num_channels;
};

// Bug 1: template base — LinuxScheduler<TimerTask> → base is "Scheduler", not "TimerTask"
struct TimerTask {
    void (*callback)();
    uint32_t period_us;
};

class LinuxScheduler : public HAL::Scheduler<TimerTask> {
public:
    void register_timer(TimerTask cb, uint32_t period_us) override;
    void delay(uint32_t ms) override;
};

// Multiple qualified bases: inherits from two NS:: types
class FullHALImpl : public HAL::AnalogIn, public HAL::AnalogSource {
public:
    HAL::AnalogSource* channel(int n) override;
    void               init()         override;
    float              read()         override;
    void               set_pin(int p) override;
};


// ── Qualified call sites (bug 3) ──────────────────────────────────────────────

namespace AP {
    void panic(const char* msg);
    int  hal_channel(int n);
}

void do_init() {
    // Bug 3: qualified call — "panic" should appear in call_sites
    AP::panic("HAL not initialised");
    int ch = AP::hal_channel(0);
    (void)ch;
}

void do_something_else() {
    AP::panic("unexpected state");
}


// ── Non-qualified calls for contrast ──────────────────────────────────────────

void standalone_init() {
    ChibiOSAnalogIn ain;
    ain.init();             // plain method call
}

HAL::AnalogSource* make_source(ChibiOSAnalogIn& ain) {
    return ain.channel(1);
}


// ── Pointer-return member function declaration (bug 4 variant) ────────────────

class SensorManager {
public:
    // Returns pointer — declarator chain: pointer_declarator → function_declarator
    virtual HAL::AnalogSource* get_source(int idx) = 0;
    virtual void               reset()             = 0;
    virtual int                count() const       = 0;
};
