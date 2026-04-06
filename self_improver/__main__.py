from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
    import subprocess
    import sys
    
    def push_code():
        try:
            subprocess.run(['git', 'push'], check=True, capture_output=True)
            print("Code pushed successfully.")
        except subprocess.CalledProcessError as e:
            print(f"Failed to push code: {e}")
            sys.exit(1)
    
    if __name__ == "__main__":
        raise SystemExit(main())
        push_code()
