# mboxsearch
Python script to search / view mbox email

## Example usage:

### SEARCH WITH MATCHING:

python mbox_search.py C:\Users\Takeout\Mail\ "i135@gmail.com" --field all


The script will process the mbox files and display a list of matching emails with their index numbers. The output will look something like this:

1. [All mail Including Spam and Trash.mbox] License renewal
2. [Important.mbox] Important license update
...


After the script displays the list of matching emails, it will prompt you:

"Enter the number of the email to view (1-<number of matches>) or 'q' to quit:"

Enter the number corresponding to the email you want to view. 

### FIELD MATCHING SUPPORT:

The script  accepts multiple search terms with optional field prefixes. For example, subject:term1 from:term2 will search for term1 in the subject and term2 in the from field.

Logical AND Condition: All specified search terms must match for a message to be considered a match.



- From:
- To:
- Subject:
- Content:
- --field all (feature flag)


### SEARCH WITH MATCHING AND LOGGING:

python mbox_search.py C:\Takeout\Mail\ "from:someone@someone.com" "subject:license" "content:important" --log search_results.log

This not only displays the search results but logs out the results to a specified log file

### SEARCH WITH EXACT MATCHING AND LOGGING:

python mbox_search.py C:\Takeout\Mail\ "from:someone@someone.com" "subject:license" "content:important" --exact --log search_results.log

This does exact (rather than fuzzy) matching of the search terms

### VIEW A SPECIFIC MESSAGE BY ITS INDEX:

python mbox_search.py C:\Takeout\Mail\ --view 128
