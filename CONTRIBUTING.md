# Contributing to Indian Stock Market Data

Thank you for your interest in contributing! This document provides guidelines and instructions for contributing.

## Code of Conduct

Please be respectful and constructive in all interactions.

## How to Contribute

### Reporting Bugs

1. **Search existing issues** to avoid duplicates
2. **Create new issue** with:
   - Clear title
   - Detailed description
   - Steps to reproduce
   - Expected vs actual behavior
   - Logs (from `logs/` directory)
   - System info (Python version, OS, etc.)

### Suggesting Enhancements

1. **Check discussions** for similar ideas
2. **Create issue** with:
   - Feature description
   - Use case
   - Benefits
   - Example implementation (optional)

### Submitting Pull Requests

1. **Fork** the repository
2. **Create branch**: `git checkout -b feature/your-feature`
3. **Make changes** with clear commits
4. **Test thoroughly** locally
5. **Submit PR** with:
   - Clear title
   - Description of changes
   - Related issue number
   - Testing notes

## Development Setup

```bash
# Clone your fork
git clone https://github.com/your-username/indian-stock-market-data.git
cd indian-stock-market-data

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac

# Install development dependencies
pip install -r requirements.txt
pip install pytest pytest-cov  # For testing

# Create feature branch
git checkout -b feature/your-feature
```

## Code Style

- Follow PEP 8
- Use meaningful variable names
- Add docstrings to functions
- Add comments for complex logic
- Keep lines under 100 characters

## Testing

```bash
# Run tests
pytest

# Run with coverage
pytest --cov=scripts --cov=utils
```

## Documentation

- Update README.md for major changes
- Update USAGE.md for new features
- Add docstrings to new functions
- Include examples where applicable

## Areas for Contribution

### High Priority
- [ ] Add more stocks to NSE_STOCKS list
- [ ] Implement database storage option
- [ ] Add data validation tests
- [ ] Create Jupyter notebook examples

### Medium Priority
- [ ] Add support for options data
- [ ] Implement caching mechanism
- [ ] Create REST API wrapper
- [ ] Add web UI dashboard

### Low Priority
- [ ] Documentation improvements
- [ ] Performance optimizations
- [ ] Code refactoring
- [ ] Additional logging

## Questions?

Feel free to:
- Open an issue
- Start a discussion
- Contact via GitHub

Thank you for contributing! 🙏
