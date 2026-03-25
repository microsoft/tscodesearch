// query_fixture.cpp — synthetic C++ file for query_cpp test suite.
// Contains no project-specific references; safe for public use.

#include <string>
#include <vector>
#include <memory>
#include <iostream>

// -- Interfaces (pure virtual) ------------------------------------------------

class IProcessor {
public:
    virtual std::string process(const std::string& input) = 0;
    virtual void reset() = 0;
    virtual ~IProcessor() = default;
};

class ILogger {
public:
    virtual void log(const std::string& message) = 0;
    virtual void warn(const std::string& message) = 0;
    virtual ~ILogger() = default;
};

// -- Struct -------------------------------------------------------------------

struct ProcessResult {
    bool success;
    std::string output;
    int errorCode;

    ProcessResult(bool s, const std::string& o, int e)
        : success(s), output(o), errorCode(e) {}
};

// -- Enum ---------------------------------------------------------------------

enum class ProcessingMode {
    Sequential,
    Parallel,
    Batch
};

// -- Abstract base class -------------------------------------------------------

class BaseProcessor : public IProcessor {
protected:
    ILogger* logger_;

public:
    explicit BaseProcessor(ILogger* logger) : logger_(logger) {}

    virtual std::string process(const std::string& input) = 0;

    void reset() override {
        // base no-op
    }
};

// -- Concrete class -----------------------------------------------------------

class TextProcessor : public BaseProcessor {
private:
    std::string prefix_;

public:
    TextProcessor(const std::string& prefix, ILogger* logger)
        : BaseProcessor(logger), prefix_(prefix) {}

    std::string format(const std::string& input) const {
        return prefix_ + input;
    }

    std::string process(const std::string& input) override {
        // COMMENT: process() is mentioned here but not a real call
        std::string result = format(input);
        logger_->log(result);
        return result;
    }

    const std::string& getPrefix() const {
        return prefix_;
    }
};

// -- Factory functions --------------------------------------------------------

TextProcessor* createProcessor(const std::string& prefix, ILogger* logger) {
    return new TextProcessor(prefix, logger);
}

ProcessResult runProcessor(IProcessor* processor, const std::string& input) {
    std::string output = processor->process(input);
    return ProcessResult(true, output, 0);
}

// -- Service class ------------------------------------------------------------

class ProcessingService {
private:
    IProcessor* processor_;
    std::vector<ProcessResult> results_;

public:
    explicit ProcessingService(IProcessor* processor) : processor_(processor) {}

    ProcessResult doWork(const std::string& input) {
        ProcessResult result = runProcessor(processor_, input);
        results_.push_back(result);
        return result;
    }

    bool inspectResult(const ProcessResult& result) {
        if (result.success) {
            std::cout << result.output << std::endl;
        }
        return result.success;
    }
};

// -- Corner cases -------------------------------------------------------------

void cornerCases() {
    // Call in string should NOT match: "process()"
    std::string s = "process() is just a string";

    // Nested calls
    ILogger* logger = nullptr;
    TextProcessor* proc = createProcessor("hello", logger);
    proc->process("world");
    ProcessResult r = runProcessor(proc, "test");
    delete proc;
}
