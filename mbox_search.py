import mailbox
import email
import argparse
import os
from tqdm import tqdm
import logging
import re

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def search_mbox(mbox_file, search_terms, exact, log_file=None):
    logging.info(f"Searching file: {mbox_file}")
    results = []
    try:
        with open(mbox_file, 'r', encoding='utf-8', errors='replace') as f:
            message = []
            index = 0
            for line in tqdm(f, desc="Processing messages"):
                if line.startswith("From "):
                    if message:
                        process_message(''.join(message), search_terms, results, mbox_file, index, exact, log_file)
                        index += 1
                    message = [line]
                else:
                    message.append(line)
            if message:
                process_message(''.join(message), search_terms, results, mbox_file, index, exact, log_file)
        logging.info(f"Found {len(results)} matches in {mbox_file}")
    except Exception as e:
        logging.error(f"Error opening or reading {mbox_file}: {str(e)}")
    
    return results

def process_message(raw_message, search_terms, results, mbox_file, index, exact, log_file=None):
    try:
        message = email.message_from_string(raw_message)
        if all(check_term(message, term, field, exact) for term, field in search_terms):
            results.append((mbox_file, index, message))
            log_message(f"Match found in message {index} from {mbox_file}: {message['subject']}\n", log_file)
    except Exception as e:
        logging.error(f"Error processing message {index} in {mbox_file}: {str(e)}")

def check_term(message, term, field, exact):
    if exact:
        pattern = re.compile(r'\b' + re.escape(term) + r'\b', re.IGNORECASE)
    else:
        pattern = re.compile(re.escape(term), re.IGNORECASE)
    
    if field == 'all' or field == 'content':
        return bool(pattern.search(message.as_string()))
    elif field == 'subject' and 'subject' in message:
        return bool(pattern.search(message['subject']))
    elif field == 'from' and 'from' in message:
        return bool(pattern.search(message['from']))
    elif field == 'to' and 'to' in message:
        return bool(pattern.search(message['to']))
    return False

def view_message(message):
    print(f"From: {message['from']}")
    print(f"To: {message['to']}")
    print(f"Subject: {message['subject']}")
    print(f"Date: {message['date']}")
    print("\nContent:")
    
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                print(part.get_payload(decode=True).decode(errors='replace'))
    else:
        print(message.get_payload(decode=True).decode(errors='replace'))
    
    input("\nPress Enter to continue...")

def log_message(message, log_file=None):
    if log_file:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(message)
    logging.info(message)

def main():
    parser = argparse.ArgumentParser(description="Search and view mbox email files.")
    parser.add_argument("mbox_dir", help="Directory containing mbox files")
    parser.add_argument("search_terms", nargs='*', help="Search terms with optional field prefixes (e.g., 'subject:term' or just 'term')")
    parser.add_argument("--field", choices=['all', 'subject', 'from', 'to', 'content'], 
                        default='all', help="Field to search in (default: all)")
    parser.add_argument("--exact", action='store_true', help="Enable exact match for search terms")
    parser.add_argument("--log", help="Log file to log matching emails")
    parser.add_argument("--view", help="View a specific message by its index", type=int)
    args = parser.parse_args()

    if args.view is not None:
        view_specific_message(args.mbox_dir, args.view)
        return

    logging.info(f"Starting search in directory: {args.mbox_dir}")
    logging.info(f"Searching for terms: {args.search_terms}")
    logging.info(f"Searching in field: {args.field}")
    logging.info(f"Exact match: {args.exact}")

    search_terms = []
    if len(args.search_terms) == 1 and args.field != 'all':
        search_terms.append((args.search_terms[0], args.field))
    else:
        for term in args.search_terms:
            if ':' in term:
                field, keyword = term.split(':', 1)
            else:
                field, keyword = 'all', term
            search_terms.append((keyword, field))

    results = []
    mbox_files = [os.path.join(root, file) 
                  for root, _, files in os.walk(args.mbox_dir) 
                  for file in files if file.endswith('.mbox')]
    
    for mbox_file in mbox_files:
        results.extend(search_mbox(mbox_file, search_terms, args.exact, args.log))

    logging.info(f"\nFound {len(results)} matching emails in total.")
    
    if results:
        while True:
            for i, (mbox_file, index, message) in enumerate(results, 1):
                print(f"{i}. [{os.path.basename(mbox_file)}] {message['subject']}\n")
            
            choice = input("\nEnter the number of the email to view (1-{}) or 'q' to quit: ".format(len(results)))
            if choice.lower() == 'q':
                break
            try:
                index = int(choice) - 1
                if 0 <= index < len(results):
                    mbox_file, msg_index, message = results[index]
                    print(f"\nViewing email from {os.path.basename(mbox_file)}, message index {msg_index}")
                    view_message(message)
                else:
                    print("Invalid number. Please try again.")
            except ValueError:
                print("Invalid input. Please enter a number or 'q'.")

def view_specific_message(mbox_dir, message_index):
    mbox_files = [os.path.join(root, file) 
                  for root, _, files in os.walk(mbox_dir) 
                  for file in files if file.endswith('.mbox')]

    found = False
    for mbox_file in mbox_files:
        logging.info(f"Checking file: {mbox_file}")
        with open(mbox_file, 'r', encoding='utf-8', errors='replace') as f:
            message = []
            index = 0
            for line in tqdm(f, desc="Reading mbox file"):
                if line.startswith("From "):
                    if message:
                        if index == message_index:
                            found = True
                            view_message(email.message_from_string(''.join(message)))
                            break
                        index += 1
                    message = [line]
                else:
                    message.append(line)
            if index == message_index and not found:
                found = True
                view_message(email.message_from_string(''.join(message)))
        if found:
            break

    if not found:
        print(f"Message with index {message_index} not found in any mbox file in directory {mbox_dir}")

if __name__ == "__main__":
    main()
